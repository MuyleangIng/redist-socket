from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import redis
import asyncio
import json
import logging
import requests
from pydantic import BaseModel
from typing import Dict, Any, Set

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Configure CORS with wildcard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS set to wildcard (*)")

# Initialize Redis client
redis_client = redis.Redis(
    host="192.168.50.144",
    port=3041,
    db=0,
    decode_responses=True
)

# Utility functions
def safe_redis_op(func, *args, **kwargs):
    """Execute a Redis operation with error handling."""
    try:
        return func(*args, **kwargs)
    except redis.RedisError as e:
        logger.error(f"Redis error in {func.__name__}: {e}")
        raise

def fetch_geolocation(ip: str) -> Dict[str, Any]:
    """Fetch geolocation data for an IP using ip-api.com."""
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}", timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "success":
            return {
                "ip": ip,
                "country": data.get("country", "Unknown"),
                "city": data.get("city", "Unknown"),
                "isp": data.get("isp", "Unknown ISP"),
                "latitude": data.get("lat", 0.0),
                "longitude": data.get("lon", 0.0),
                "browser": "Unknown"
            }
        logger.warning(f"Geolocation failed for IP {ip}: {data.get('message')}")
    except requests.RequestException as e:
        logger.error(f"Geolocation error for IP {ip}: {e}")
    return {"ip": ip, "country": "Unknown", "city": "Unknown", "isp": "Unknown ISP", "latitude": 0.0, "longitude": 0.0, "browser": "Unknown"}

def increment_counter(key: str):
    """Increment a Redis counter."""
    safe_redis_op(redis_client.incr, key)

def add_to_set(key: str, value: str):
    """Add a value to a Redis set if valid."""
    if value and isinstance(value, str) and not value.startswith('{'):
        safe_redis_op(redis_client.sadd, key, value)
        logger.info(f"Added {value} to {key}")

# Clear countries data on startup
try:
    safe_redis_op(redis_client.delete, "countries")
except redis.RedisError:
    pass  # Already logged in safe_redis_op

# Endpoints
class IPData(BaseModel):
    ip: str
    country: str
    city: str
    isp: str
    latitude: float
    longitude: float
    browser: str

@app.post("/track/pageview/{service}")
async def track_pageview(service: str, request: Request):
    """Track a pageview for a service."""
    try:
        data = await request.json()
        page = data.get('page', '/')
        ip = data.get('ip', request.client.host)

        country_info = fetch_geolocation(ip)
        increment_counter(f"{service}:pageview:{page}")
        safe_redis_op(redis_client.set, f"{service}_ip_data:{ip}", json.dumps(country_info))
        add_to_set(f"{service}:unique_ips", ip)

        logger.info(f"Pageview tracked: {service} - {page}, IP: {ip}")
        return {"status": "pageview tracked", "service": service, "country_info": country_info}
    except Exception as e:
        logger.error(f"Pageview tracking error: {e}")
        return {"error": "Internal Server Error"}, 500

@app.post("/track/time/{service}")
async def track_time(service: str, request: Request):
    """Track time spent on a page for a service."""
    try:
        data = await request.json()
        page = data.get('page', '/')
        time_spent = int(data.get('timeSpent', 0))

        if time_spent < 0:
            return {"error": "Invalid timeSpent value"}, 400

        safe_redis_op(redis_client.incrby, f"{service}:time:{page}", time_spent)
        logger.info(f"Time tracked: {service} - {page}, {time_spent}s")

        stats = get_stats()
        await broadcast_stats(stats)

        return {"status": "time tracked", "service": service, "page": page, "timeSpent": time_spent}
    except ValueError:
        return {"error": "Invalid timeSpent value"}, 400
    except Exception as e:
        logger.error(f"Time tracking error: {e}")
        return {"error": "Internal Server Error"}, 500

@app.post("/login/{service}")
async def login_post(service: str, ip_data: IPData):
    """Record a login event for a service."""
    try:
        safe_redis_op(redis_client.set, f"{service}_ip_data:{ip_data.ip}", json.dumps(ip_data.dict()))
        add_to_set(f"{service}:unique_ips", ip_data.ip)
        logger.info(f"Login recorded: {service} - IP: {ip_data.ip}")
        return ip_data.dict()
    except Exception as e:
        logger.error(f"Login error: {e}")
        return {"error": "Internal Server Error"}, 500

@app.get("/stats")
def get_stats() -> Dict[str, Any]:
    """Retrieve stats for all services."""
    services = ['Qummit', 'QuSpace', 'QuMatics']
    stats = {}

    for service in services:
        pages = safe_redis_op(redis_client.keys, f"{service}:pageview:*")
        for page in pages:
            page_name = page.split(':')[-1]
            stats[f"{service}:pageview:{page_name}"] = int(safe_redis_op(redis_client.get, page) or 0)
            stats[f"{service}:time:{page_name}"] = int(safe_redis_op(redis_client.get, f"{service}:time:{page_name}") or 0)

        unique_ips = safe_redis_op(redis_client.smembers, f"{service}:unique_ips") or set()
        stats[f"{service}:countries"] = [
            json.loads(safe_redis_op(redis_client.get, f"{service}_ip_data:{ip}"))
            for ip in unique_ips
            if safe_redis_op(redis_client.get, f"{service}_ip_data:{ip}")
        ]

    logger.info("Stats retrieved")
    return stats

# WebSocket management
active_connections: Set[WebSocket] = set()

async def broadcast_stats(stats: Dict[str, Any]):
    """Broadcast stats to active WebSocket connections."""
    for conn in active_connections.copy():
        try:
            await conn.send_json(stats)
        except Exception as e:
            logger.error(f"WebSocket broadcast error: {e}")
            active_connections.remove(conn)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connections for real-time stats."""
    await websocket.accept()
    active_connections.add(websocket)
    try:
        while True:
            stats = get_stats()
            await websocket.send_json(stats)
            await asyncio.sleep(5)
    except (WebSocketDisconnect, Exception) as e:
        logger.error(f"WebSocket error: {e}")
        active_connections
