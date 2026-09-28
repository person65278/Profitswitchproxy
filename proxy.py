import asyncio
import json
import logging
import socket
import requests

# ==========================================================
# CONFIGURATION
# Running and shutting this down rapidly will get you a 5 minute cooldown ban on kryptex. 
# Change Spots that say YOUR KRYPTEX USER
# ==========================================================
PROXY_LISTEN_HOST = "0.0.0.0"
PROXY_LISTEN_PORT = 3333

CHECK_INTERVAL_SECONDS = 3600  # 1 Hour Change as you see fit.

# Miner Profitability Calculation Parameters
ESTIMATED_HASHRATE_TH = 200      # 200 TH/s (Antminer S21)
POWER_CONSUMPTION_WATTS = 3500   # 3.5 kW
ELECTRICITY_COST_PER_KWH = 0.06  # Electricity cost in USD/kWh

# Kryptex Stratum Pool Endpoints for SHA-256 coins
POOLS = {
    "BTC":  {"host": "btc.kryptex.network",         "port": 7014, "user": "YOUR KRYPTEX USER.BTCproxy",  "pass": "x"},
    "FB":   {"host": "fb.kryptex.network",          "port": 7013, "user": "YOUR KRYPTEX USER.FBproxy",   "pass": "x"},
    "BCH":  {"host": "bch.kryptex.network",         "port": 7015, "user": "YOUR KRYPTEX USER.BCHproxy",  "pass": "x"},
    "BSV":  {"host": "bsv.kryptex.network",         "port": 7047, "user": "YOUR KRYPTEX USER.BSVproxy",  "pass": "x"},
    "DGB":  {"host": "dgb.kryptex.network",         "port": 7037, "user": "YOUR KRYPTEX USER.DGBproxy",  "pass": "x"},
    "QUAI": {"host": "quai-sha256.kryptex.network", "port": 7044, "user": "YOUR KRYPTEX USER.QUAIproxy", "pass": "x"},
}

# Standardized SHA-256 Network Fallbacks
COIN_PARAMS = {
    "BTC":  {"default_diff": 85000000000000, "block_reward": 3.125},
    "FB":   {"default_diff": 450000000000,   "block_reward": 25.0},
    "BCH":  {"default_diff": 380000000000,   "block_reward": 3.125},
    "BSV":  {"default_diff": 22000000000,    "block_reward": 3.125},
    "DGB":  {"default_diff": 120000000,      "block_reward": 600.0},
    "QUAI": {"default_diff": 180000000000000000, "block_reward": 4.95},
}

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

# ==========================================================
# GLOBAL STATE (SINGLE-MINER ARCHITECTURE)
# ==========================================================
class ProxyState:
    def __init__(self):
        self.active_coin = "BTC"
        self.miner_writer = None
        self.miner_username = None
        self.miner_subscribed = False
        self.miner_authorized = False
        self.pool_reader = None
        self.pool_writer = None
        self.listener_task = None
        self.extranonce1 = "00000000"
        self.extranonce2_size = 4
        self.active_jobs = set()
        self.session_id = 0
        self.ranked_coins = ["BTC", "FB", "BCH", "BSV", "DGB", "QUAI"]
        self.switch_lock = asyncio.Lock()
        self.pending_submissions = {}

state = ProxyState()

def enable_tcp_keepalive(writer):
    try:
        sock = writer.get_extra_info('socket')
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, 'TCP_KEEPIDLE'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            if hasattr(socket, 'TCP_KEEPINTVL'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            if hasattr(socket, 'TCP_KEEPCNT'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except Exception as e:
        logging.debug(f"Could not enable TCP keepalive: {e}")

# ==========================================================
# PROFITABILITY ENGINE
# ==========================================================
def get_ranked_profitable_coins():
    daily_power_cost = (POWER_CONSUMPTION_WATTS / 1000.0) * 24.0 * ELECTRICITY_COST_PER_KWH
    scores = {}
    session = requests.Session()

    cg_id_map = {
        "BTC":  "bitcoin",
        "FB":   "fractal-bitcoin",
        "BCH":  "bitcoin-cash",
        "BSV":  "bitcoin-cash-sv",
        "DGB":  "digibyte",
        "QUAI": "quai-network"
    }
    
    prices = {coin: 0.0 for coin in COIN_PARAMS}
    try:
        cg_ids = ",".join(cg_id_map.values())
        cg_url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_ids}&vs_currencies=usd"
        cg_res = session.get(cg_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
        
        for coin, cg_id in cg_id_map.items():
            if cg_id in cg_res and "usd" in cg_res[cg_id]:
                prices[coin] = float(cg_res[cg_id]["usd"])
    except Exception as e:
        logging.error(f"CoinGecko price fetch failed: {e}")

    kryptex_stats = {}
    try:
        k_res = session.get("https://pool.kryptex.com/api/v1/coins", timeout=10).json()
        if isinstance(k_res, list):
            for item in k_res:
                symbol = item.get("coin", item.get("symbol", "")).upper()
                kryptex_stats[symbol] = item
    except Exception as e:
        logging.error(f"Kryptex stats fetch failed: {e}")

    logging.info("--- [KRYPTEX ALL SHA-256 200 TH/s PROFITABILITY] ---")
    logging.info(f"Daily Electricity Cost @ ${ELECTRICITY_COST_PER_KWH}/kWh: ${daily_power_cost:.2f}/day")

    for coin in ["BTC", "FB", "BCH", "BSV", "DGB", "QUAI"]:
        params = COIN_PARAMS[coin]
        c_info = kryptex_stats.get(coin, {})

        price = prices.get(coin, 0.0)

        raw_diff = float(c_info.get("difficulty", c_info.get("network_difficulty", 0)))
        diff = raw_diff if raw_diff > 0 else params["default_diff"]

        reward = float(c_info.get("reward", c_info.get("block_reward", params["block_reward"])))

        hashes_per_second = ESTIMATED_HASHRATE_TH * 1e12
        
        if coin == "QUAI":
            daily_coins = (hashes_per_second / diff) * 86400 * reward
            calc_diff = diff / 86400.0
        elif coin == "DGB":
            hashes_per_day = hashes_per_second * 86400
            expected_blocks = hashes_per_day / ((diff * 1000.0) * (2**32))
            daily_coins = expected_blocks * reward
            calc_diff = diff * 1000.0
        else:
            hashes_per_day = hashes_per_second * 86400
            expected_blocks = hashes_per_day / (diff * (2**32))
            daily_coins = expected_blocks * reward
            calc_diff = diff

        gross_daily_usd = daily_coins * price
        net_daily_usd = gross_daily_usd - daily_power_cost

        score = (reward * price) / calc_diff if price > 0 and calc_diff > 0 else 0.0
        scores[coin] = score

        price_str = f"${price:,.4f}" if price < 1.0 else f"${price:,.2f}"
        coins_str = f"{daily_coins:,.2f}" if daily_coins > 10 else f"{daily_coins:,.4f}"
        coin_label = f"{coin:<4}"

        logging.info(
            f" -> {coin_label} | Price: {price_str:>10} | Est: {coins_str:>12} {coin}/day "
            f"| Gross: ${gross_daily_usd:>6.2f}/day | NET: ${net_daily_usd:>+6.2f}/day"
        )

    if any(s > 0 for s in scores.values()):
        ranked = sorted(scores, key=scores.get, reverse=True)
        logging.info(f"Kryptex Profitability Ranking: {' > '.join(ranked)}")
        logging.info("-------------------------------------------------")
        return ranked

    return state.ranked_coins

async def profitability_loop():
    while True:
        try:
            logging.info("Checking Kryptex Pool live coin profitability metrics...")
            state.ranked_coins = get_ranked_profitable_coins()
            top_coin = state.ranked_coins[0]
            
            if top_coin != state.active_coin:
                logging.info(f"PROFIT SWITCH TRIGGERED: Target change to {top_coin}")
                await connect_with_failover(state.ranked_coins)
            else:
                logging.info(f"Target pool remains {state.active_coin}.")
        except Exception as e:
            logging.error(f"Error in profitability loop: {e}")
            
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ==========================================================
# STRATUM POOL CONNECTION MANAGEMENT
# ==========================================================
async def close_current_pool():
    if state.listener_task:
        state.listener_task.cancel()
        try:
            await state.listener_task
        except asyncio.CancelledError:
            pass
        state.listener_task = None

    if state.pool_writer:
        try:
            state.pool_writer.close()
            await state.pool_writer.wait_closed()
        except Exception:
            pass
    state.pool_reader = None
    state.pool_writer = None

async def connect_to_pool(coin):
    pool = POOLS[coin]
    logging.info(f"Connecting to stratum pool: {pool['host']}:{pool['port']} ({coin})...")
    
    state.active_jobs.clear()
    state.pending_submissions.clear()
    
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(pool['host'], pool['port']), timeout=10
        )
        
        enable_tcp_keepalive(writer)
        
        sub_req = json.dumps({
            "id": 1,
            "method": "mining.subscribe",
            "params": ["Antminer S21 Stratum Proxy/1.0"]
        }) + "\n"
        writer.write(sub_req.encode())
        await writer.drain()
        
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not line:
            writer.close()
            return False
            
        res = json.loads(line.decode().strip())
        if res.get("result"):
            state.extranonce1 = res["result"][1]
            state.extranonce2_size = res["result"][2]
            logging.info(f"Subscribed to Pool [{coin}]. ExtraNonce1: {state.extranonce1}")

        auth_req = json.dumps({
            "id": 2,
            "method": "mining.authorize",
            "params": [pool['user'], pool['pass']]
        }) + "\n"
        writer.write(auth_req.encode())
        await writer.drain()
        
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not line:
            writer.close()
            return False

        auth_res = json.loads(line.decode().strip())
        if auth_res.get("result") is True:
            logging.info(f"Successfully Authorized worker '{pool['user']}' on {coin} Pool.")
            
            state.active_coin = coin
            state.session_id += 1
            
            state.pool_reader = reader
            state.pool_writer = writer
            state.listener_task = asyncio.create_task(listen_to_pool(reader, coin, state.session_id))
            return True
        else:
            logging.error(f"Authorization rejected on {coin} pool: {auth_res}")
            writer.close()
            return False

    except (json.JSONDecodeError, Exception) as e:
        logging.error(f"Failed to connect to pool {coin}: {e}")
        return False

async def connect_with_failover(ranked_list):
    async with state.switch_lock:
        await close_current_pool()

        for coin in ranked_list:
            logging.info(f"Attempting connection to {coin} pool...")
            await asyncio.sleep(2)
            
            success = await connect_to_pool(coin)
            if success:
                logging.info(f"ACTIVE POOL SET TO: {coin} (Session #{state.session_id})")
                return True
            else:
                logging.warning(f"Connection to {coin} failed! Falling back...")

        logging.critical("ALL POOLS UNREACHABLE! Retrying primary in 15 seconds...")
        await asyncio.sleep(15)
        return await connect_with_failover(ranked_list)

async def listen_to_pool(reader, coin_name, current_session):
    try:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=120.0)
            except asyncio.TimeoutError:
                logging.warning(f"Pool connection idle timeout reached for {coin_name}. Reconnecting...")
                break

            if not line:
                logging.warning(f"Pool connection dropped for {coin_name}.")
                break
            
            raw_str = line.decode().strip()
            if not raw_str or not raw_str.startswith('{'):
                continue
                
            try:
                data = json.loads(raw_str)
            except json.JSONDecodeError:
                continue
            
            if data.get("method") == "mining.notify":
                params = data.get("params", [])
                if params:
                    job_id = params[0]
                    if len(params) > 8 and params[8] is True:
                        state.active_jobs.clear()
                    
                    state.active_jobs.add(job_id)
                    logging.info(f"Incoming Pool Job #{job_id} [{coin_name}] -> Forwarding to S21")
                    
                    if state.miner_writer:
                        try:
                            state.miner_writer.write(line)
                            await state.miner_writer.drain()
                        except Exception:
                            pass
            
            elif data.get("method") == "mining.set_difficulty":
                logging.info(f"Pool adjusted target difficulty for {coin_name}: {data.get('params')}")
                if state.miner_writer:
                    try:
                        state.miner_writer.write(line)
                        await state.miner_writer.drain()
                    except Exception:
                        pass
                    
            elif "result" in data and data.get("id") is not None:
                req_id = data.get("id")
                
                if req_id in state.pending_submissions:
                    del state.pending_submissions[req_id]
                    
                    data["result"] = True
                    data["error"] = None
                    
                    if state.miner_writer:
                        try:
                            fake_res_line = json.dumps(data) + "\n"
                            state.miner_writer.write(fake_res_line.encode())
                            await state.miner_writer.drain()
                        except Exception:
                            pass
                    continue

                if state.miner_writer:
                    try:
                        state.miner_writer.write(line)
                        await state.miner_writer.drain()
                    except Exception:
                        pass

    except asyncio.CancelledError:
        return
    except Exception as e:
        logging.error(f"Error reading from pool ({coin_name}): {e}")

    if state.session_id == current_session and not state.switch_lock.locked():
        fallback_list = [c for c in state.ranked_coins if c != coin_name] + [coin_name]
        asyncio.create_task(connect_with_failover(fallback_list))

# ==========================================================
# MINER SERVER HANDLER (SINGLE ANTMINER S21 ROUTER)
# ==========================================================
async def handle_miner_client(reader, writer):
    peer = writer.get_extra_info('peername', ('unknown', 0))
    logging.info(f"Antminer S21 connected from {peer[0]}:{peer[1]}")
    
    enable_tcp_keepalive(writer)
    state.miner_writer = writer

    try:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=180.0)
            except asyncio.TimeoutError:
                logging.warning("S21 miner timed out due to prolonged inactivity. Waiting for reconnect...")
                break

            if not line:
                logging.warning("S21 miner disconnected cleanly.")
                break

            raw_str = line.decode().strip()
            if not raw_str or not raw_str.startswith('{'):
                continue

            try:
                msg = json.loads(raw_str)
            except json.JSONDecodeError:
                continue

            method = msg.get("method")
            msg_id = msg.get("id")

            if method == "mining.subscribe":
                logging.info("Handshaking: Received mining.subscribe from S21.")
                state.miner_subscribed = True
                resp = json.dumps({
                    "id": msg_id,
                    "result": [
                        [["mining.set_difficulty", "1"], ["mining.notify", "1"]],
                        state.extranonce1,
                        state.extranonce2_size
                    ],
                    "error": None
                }) + "\n"
                writer.write(resp.encode())
                await writer.drain()

                diff_resp = json.dumps({
                    "id": None,
                    "method": "mining.set_difficulty",
                    "params": [512]
                }) + "\n"
                writer.write(diff_resp.encode())
                await writer.drain()

            elif method == "mining.authorize":
                params = msg.get("params", [])
                if params:
                    state.miner_username = params[0]
                    logging.info(f"Handshaking: Received mining.authorize for worker '{state.miner_username}'")
                
                state.miner_authorized = True
                resp = json.dumps({"id": msg_id, "result": True, "error": None}) + "\n"
                writer.write(resp.encode())
                await writer.drain()

            elif method == "mining.submit":
                params = msg.get("params", [])
                job_id = params[1] if len(params) > 1 else None
                
                # PRE-SUBMISSION VALIDATION: Check if job matches current active coin & session
                if not job_id or job_id not in state.active_jobs:
                    logging.warning(f"MISMATCHED / STALE SHARE BLOCKED for Job #{job_id} on coin {state.active_coin}. Faking success response to S21.")
                    
                    reject_resp = json.dumps({
                        "id": msg_id,
                        "result": True,
                        "error": None
                    }) + "\n"
                    writer.write(reject_resp.encode())
                    await writer.drain()
                    continue

                logging.info(f"S21 Share Submitted for Valid Job #{job_id} [{state.active_coin}] (Tracking ID {msg_id})")
                
                if msg_id is not None:
                    state.pending_submissions[msg_id] = True

                if len(params) >= 5 and state.pool_writer:
                    params[0] = state.miner_username if state.miner_username else POOLS[state.active_coin]["user"]
                    
                    forward_msg = json.dumps({
                        "id": msg_id,
                        "method": "mining.submit",
                        "params": params
                    }) + "\n"
                    try:
                        state.pool_writer.write(forward_msg.encode())
                        await state.pool_writer.drain()
                    except Exception as e:
                        logging.error(f"Failed to relay share submission to pool: {e}")

            elif state.pool_writer:
                try:
                    state.pool_writer.write(line)
                    await state.pool_writer.drain()
                except Exception:
                    pass

    except (ConnectionError, OSError) as e:
        logging.warning(f"S21 miner connection abruptly lost ({e}). Waiting for reconnect...")
    except Exception as e:
        logging.error(f"Error handling miner socket: {e}")
    finally:
        state.miner_writer = None
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# ==========================================================
# MAIN ENTRYPOINT
# ==========================================================
async def main():
    print("=========================================================")
    print("    ANTMINER S21 AUTOMATED STRATUM PROXY SWITCHER      ")
    print("=========================================================")
    
    logging.info("Initializing primary pool connection...")
    state.ranked_coins = get_ranked_profitable_coins()
    await connect_with_failover(state.ranked_coins)

    asyncio.create_task(profitability_loop())

    server = await asyncio.start_server(
        handle_miner_client, PROXY_LISTEN_HOST, PROXY_LISTEN_PORT
    )
    
    logging.info(f"Stratum Proxy Server listening on {PROXY_LISTEN_HOST}:{PROXY_LISTEN_PORT}")
    
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Stratum Proxy Server Stopped.")
