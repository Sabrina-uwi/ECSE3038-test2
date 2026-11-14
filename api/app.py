
import os
import secrets
from datetime import datetime, timezone
from typing import Literal

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, DESCENDING
from pymongo.errors import DuplicateKeyError


# --------------------------------------------------
# ENVIRONMENT VARIABLES
# --------------------------------------------------

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MQTT_NAME = os.getenv("MQTT_NAME", "probiotic")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is missing from .env")

MQTT_BROKER = "135.232.203.144"
MQTT_PORT = 1883

if not MQTT_NAME.isalnum() or MQTT_NAME.lower() != MQTT_NAME:
    raise RuntimeError("MQTT_NAME must contain lowercase letters and digits only")


# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------

mongo_client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=5000
)

db = mongo_client["ECSE3038test2"]

fans_collection = db["fans"]
readings_collection = db["readings"]

# MAC addresses must be unique.
fans_collection.create_index("mac", unique=True)


# --------------------------------------------------
# FASTAPI APPLICATION
# --------------------------------------------------

app = FastAPI(title="ECSE3038 Bathroom Fan API")


# --------------------------------------------------
# PYDANTIC MODELS
# --------------------------------------------------

class FanCreate(BaseModel):
    mac: str = Field(min_length=1)
    room: str = Field(min_length=1)


class ReadingCreate(BaseModel):
    mac: str = Field(min_length=1)
    humidity: float = Field(ge=0, le=100)


class FanSettings(BaseModel):
    mode: Literal["auto", "on", "off"]
    max_humidity: float = Field(ge=0, le=100)


# --------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------

def normalize_mac(mac: str) -> str:
    return mac.strip().lower()


def public_fan(fan):
    """Remove private API key and MongoDB ID."""
    fan = dict(fan)
    fan.pop("_id", None)
    fan.pop("api_key", None)
    return fan


def authenticate_fan(mac: str, api_key: str | None):
    """
    Check whether an API key exists and belongs
    to the requested fan.
    """

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key"
        )

    fan = fans_collection.find_one({
        "api_key": api_key
    })

    if fan is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )

    if fan["mac"] != normalize_mac(mac):
        raise HTTPException(
            status_code=403,
            detail="API key belongs to another fan"
        )

    return fan


def publish_settings(mac: str, settings: dict):
    """Publish retained fan settings using MQTT."""

    topic = f"ecse3038/{MQTT_NAME}/fan/settings"

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION1
    )

    try:
        client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            60
        )

        client.loop_start()

        info = client.publish(
            topic,
            payload=__import__("json").dumps(settings),
            qos=1,
            retain=True
        )

        info.wait_for_publish(timeout=5)

        if not info.is_published():
            raise RuntimeError("MQTT publish timed out")

        client.loop_stop()
        client.disconnect()

    except Exception as error:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

        raise HTTPException(
            status_code=503,
            detail=f"MQTT publishing failed: {str(error)}"
        )


# --------------------------------------------------
# A1: REGISTER AND RETRIEVE FANS
# --------------------------------------------------

@app.post("/fans", status_code=201)
def create_fan(fan: FanCreate):

    mac = normalize_mac(fan.mac)

    api_key = secrets.token_urlsafe(16)

    new_fan = {
        "mac": mac,
        "room": fan.room,
        "api_key": api_key
    }

    try:
        fans_collection.insert_one(new_fan)

    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Fan already exists"
        )

    return {
        "mac": mac,
        "room": fan.room,
        "api_key": api_key
    }


@app.get("/fans")
def get_fans():

    fans = fans_collection.find(
        {},
        {"api_key": 0}
    )

    return [
        public_fan(fan)
        for fan in fans
    ]


@app.get("/fans/{mac}")
def get_fan(mac: str):

    fan = fans_collection.find_one({
        "mac": normalize_mac(mac)
    })

    if fan is None:
        raise HTTPException(
            status_code=404,
            detail="Fan not found"
        )

    return public_fan(fan)


# --------------------------------------------------
# A2: RECEIVE HUMIDITY READINGS
# --------------------------------------------------

@app.post("/readings", status_code=201)
def create_reading(
    reading: ReadingCreate,
    api_key: str | None = Header(
        default=None,
        alias="X-API-Key"
    )
):

    mac = normalize_mac(reading.mac)

    authenticate_fan(mac, api_key)

    new_reading = {
        "mac": mac,
        "humidity": reading.humidity,
        "timestamp": datetime.now(timezone.utc)
    }

    readings_collection.insert_one(new_reading)

    return {
        "message": "Reading saved",
        "mac": mac,
        "humidity": reading.humidity,
        "timestamp": new_reading["timestamp"]
    }


# --------------------------------------------------
# A3: RETRIEVE READINGS
# --------------------------------------------------

@app.get("/readings")
def get_readings(
    mac: str | None = None,
    limit: int = Query(
        default=10,
        ge=1,
        le=100
    )
):

    query = {}

    if mac:
        query["mac"] = normalize_mac(mac)

    readings = readings_collection.find(
        query
    ).sort(
        "timestamp",
        DESCENDING
    ).limit(limit)

    results = []

    for reading in readings:
        reading["_id"] = str(reading["_id"])
        results.append(reading)

    return results


# --------------------------------------------------
# A4: UPDATE FAN SETTINGS
# --------------------------------------------------

@app.put("/fans/{mac}/settings")
def update_fan_settings(
    mac: str,
    settings: FanSettings
):

    mac = normalize_mac(mac)

    fan = fans_collection.find_one({
        "mac": mac
    })

    if fan is None:
        raise HTTPException(
            status_code=404,
            detail="Fan not found"
        )

    is_new = not (
        "mode" in fan and
        "max_humidity" in fan
    )

    settings_data = {
        "mode": settings.mode,
        "max_humidity": settings.max_humidity
    }

    fans_collection.update_one(
        {"mac": mac},
        {"$set": settings_data}
    )

    # Publish the new settings to the board.
    publish_settings(mac, settings_data)

    status_code = 201 if is_new else 200

    return JSONResponse(
        status_code=status_code,
        content={
            "mac": mac,
            "mode": settings.mode,
            "max_humidity": settings.max_humidity
        }
    )


# --------------------------------------------------
# A5: DELETE FAN
# --------------------------------------------------

@app.delete("/fans/{mac}", status_code=204)
def delete_fan(
    mac: str,
    api_key: str | None = Header(
        default=None,
        alias="X-API-Key"
    )
):

    mac = normalize_mac(mac)

    fan = fans_collection.find_one({
        "mac": mac
    })

    if fan is None:
        raise HTTPException(
            status_code=404,
            detail="Fan not found"
        )

    authenticate_fan(mac, api_key)

    fans_collection.delete_one({
        "mac": mac
    })

    return None
