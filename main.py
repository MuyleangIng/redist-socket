from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import redis
import asyncio
import json
import time
import requests
import logging
from pydantic import BaseModel

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_client = redis.Redis(host='136.228.158.126', port=3041, db=0, decode_responses=True)

# Clear existing country data on startup
redis_client.delete("countries")

def increment_counter(key):
    redis_client.incr(key)

def add_to_set(key, value):
    if value and isinstance(value, str) and not value.startswith('{'):
        redis_client.sadd(key, value)
        logger.info(f"Added {value} to {key}")

@app.post("/track/pageview/{service}")
async def track_pageview(service: str, request: Request):
    data = await request.json()
    page = data.get('page', '/')
    # Use the provided IP if available, otherwise fall back to request.client.host
    ip = data.get('ip', request.client.host)
    
    country_info = {
        "ip": ip,
        "country": data.get("country", "Unknown"),
        "city": data.get("city", "Unknown"),
        "isp": data.get("isp", "Unknown ISP"),
        "latitude": data.get("latitude", 0),
        "longitude": data.get("longitude", 0),
        "browser": data.get("browser", "Unknown")
    }
    
    # Increment pageview counter
    increment_counter(f"{service}:pageview:{page}")
    # Store IP data
    redis_client.set(f"{service}_ip_data:{ip}", json.dumps(country_info))
    # Add IP to unique_ips set for the service
    add_to_set(f"{service}:unique_ips", ip)
    
    logger.info(f"Pageview tracked for {service} - {page}, IP: {ip}, Country Info: {country_info}")
    return {"status": "pageview tracked", "service": service, "country_info": country_info}
@app.post("/track/time/{service}")
async def track_time(service: str, request: Request):
    try:
        data = await request.json()
        page = data.get('page', '/')
        time_spent = int(data.get('timeSpent', 0))  # Ensure it's an integer

        if time_spent < 0:
            return {"error": "Invalid timeSpent value. Must be positive."}

        redis_client.incrby(f"{service}:time:{page}", time_spent)
        logger.info(f"Time tracked for {service} - {page}: {time_spent} seconds")

        # WebSocket Broadcast (if connections exist)
        stats = get_stats()
        if active_connections:
            for connection in active_connections.copy():
                try:
                    await connection.send_json(stats)
                except Exception as e:
                    logger.error(f"WebSocket error: {e}")
                    active_connections.remove(connection)

        return {"status": "time tracked", "service": service, "page": page, "timeSpent": time_spent}

    except Exception as e:
        logger.error(f"Error tracking time: {e}")
        return {"error": "Internal Server Error"}, 500

class IPData(BaseModel):
    ip: str
    country: str
    city: str
    isp: str
    latitude: float
    longitude: float
    browser: str

@app.post("/login/{service}")  # Made service-specific
async def login_post(service: str, ip_data: IPData):
    # Store IP data under service-specific key
    redis_client.set(f"{service}_ip_data:{ip_data.ip}", json.dumps(ip_data.dict()))
    # Add IP to unique_ips set for the service
    add_to_set(f"{service}:unique_ips", ip_data.ip)
    logger.info(f"Login recorded for {service} - IP: {ip_data.ip}")
    return ip_data.dict()

@app.get("/stats")
def get_stats():
    services = ['Qummit', 'QuSpace', 'QuMatics']
    stats = {}

    for service in services:
        pages = redis_client.keys(f"{service}:pageview:*")
        for page in pages:
            page_name = page.split(':')[-1]
            stats[f"{service}:pageview:{page_name}"] = int(redis_client.get(page) or 0)
            stats[f"{service}:time:{page_name}"] = int(redis_client.get(f"{service}:time:{page_name}") or 0)
        
        # Get unique IPs for the service and fetch their country data
        unique_ips = redis_client.smembers(f"{service}:unique_ips") or set()
        stats[f"{service}:countries"] = [
            json.loads(redis_client.get(f"{service}_ip_data:{ip}"))
            for ip in unique_ips
            if redis_client.get(f"{service}_ip_data:{ip}")
        ]
    
    logger.info(f"Retrieved stats: {stats}")
    return stats

# WebSocket management
active_connections = set()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    try:
        while True:
            stats = get_stats()
            await websocket.send_json(stats)
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        active_connections.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3039)
