import os
import secrets
import json
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, DESCENDING
from dotenv import load_dotenv
import paho.mqtt.publish as mqtt_publish

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MQTT_NAME = os.getenv("MQTT_NAME")

BROKER_HOST = "135.232.203.144"
BROKER_PORT = 1883

client = MongoClient(MONGODB_URI)
db = client.ecse3038
fans = db.fans
readings = db.readings

app = FastAPI()


# ---------- Models ----------
class FanIn(BaseModel):
    mac: str
    room: str


class ReadingIn(BaseModel):
    mac: str
    humidity: float


class SettingsIn(BaseModel):
    mode: str
    max_humidity: float


# ---------- Helpers ----------
def strip_key(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("key", None)
    return doc


def publish_settings(mode: str, max_humidity: float):
    topic = f"ecse3038/{MQTT_NAME}/fan/settings"
    payload = json.dumps({"mode": mode, "max_humidity": float(max_humidity)})
    mqtt_publish.single(
        topic,
        payload=payload,
        qos=1,
        retain=True,
        hostname=BROKER_HOST,
        port=BROKER_PORT,
    )


# ---------- A1: Fan registry ----------
@app.post("/fans", status_code=201)
def create_fan(body: FanIn):
    if fans.find_one({"mac": body.mac}):
        raise HTTPException(status_code=409, detail="MAC already exists")
    key = secrets.token_urlsafe(16)
    fans.insert_one({"mac": body.mac, "room": body.room, "key": key})
    return {"mac": body.mac, "room": body.room, "key": key}


@app.get("/fans")
def list_fans():
    return [strip_key(f) for f in fans.find()]


@app.get("/fans/{mac}")
def get_fan(mac: str):
    fan = fans.find_one({"mac": mac})
    if not fan:
        raise HTTPException(status_code=404, detail="Fan not found")
    return strip_key(fan)


# ---------- A2: POST /readings ----------
@app.post("/readings", status_code=201)
def add_reading(body: ReadingIn, x_api_key: str = Header(default=None)):
    if x_api_key is None:
        raise HTTPException(status_code=401, detail="Missing key")

    fan = fans.find_one({"key": x_api_key})
    if not fan:
        raise HTTPException(status_code=401, detail="Unknown key")
    if fan["mac"] != body.mac:
        raise HTTPException(status_code=403, detail="Key belongs to another MAC")

    if body.humidity < 0 or body.humidity > 100:
        raise HTTPException(status_code=422, detail="Humidity out of range")

    doc = {
        "mac": body.mac,
        "humidity": body.humidity,
        "time": datetime.now(timezone.utc).isoformat(),
    }
    readings.insert_one(doc)
    doc.pop("_id", None)
    return doc


# ---------- A3: GET /readings ----------
@app.get("/readings")
def get_readings(mac: str | None = None, limit: int = 10):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be 1..100")

    query = {}
    if mac is not None:
        query["mac"] = mac

    cursor = readings.find(query).sort("time", DESCENDING).limit(limit)
    out = []
    for r in cursor:
        r.pop("_id", None)
        out.append(r)
    return out


# ---------- A4: PUT /fans/{mac}/settings ----------
@app.put("/fans/{mac}/settings")
def put_settings(mac: str, body: SettingsIn, response: Response):
    if body.mode not in ("auto", "on", "off"):
        raise HTTPException(status_code=422, detail="mode must be auto/on/off")
    if body.max_humidity < 0 or body.max_humidity > 100:
        raise HTTPException(status_code=422, detail="max_humidity 0..100")

    fan = fans.find_one({"mac": mac})
    if not fan:
        raise HTTPException(status_code=404, detail="Fan not found")

    existing = fans.find_one({"mac": mac, "settings": {"$exists": True}})
    status = 200 if existing else 201

    fans.update_one(
        {"mac": mac},
        {"$set": {"settings": {"mode": body.mode, "max_humidity": body.max_humidity}}},
    )
    publish_settings(body.mode, body.max_humidity)
    response.status_code = status
    return {"mode": body.mode, "max_humidity": body.max_humidity}


# ---------- A5: DELETE /fans/{mac} ----------
@app.delete("/fans/{mac}", status_code=204)
def delete_fan(mac: str, x_api_key: str = Header(default=None)):
    fan = fans.find_one({"mac": mac})
    if not fan:
        raise HTTPException(status_code=404, detail="Fan not found")

    if x_api_key is None:
        raise HTTPException(status_code=401, detail="Missing key")
    if x_api_key != fan["key"]:
        # Distinguish: does this key belong to ANY fan?
        other = fans.find_one({"key": x_api_key})
        if other is None:
            raise HTTPException(status_code=401, detail="Unknown key")
        raise HTTPException(status_code=403, detail="Key belongs to another fan")

    fans.delete_one({"mac": mac})
    return Response(status_code=204)