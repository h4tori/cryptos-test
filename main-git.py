#!/usr/bin/env python3
import asyncio
import json
from typing import List, Optional

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from gino import Gino


# Konfigurace připojení k databázi a Redis
DATABASE_URL = "postgres://avnadmin:XXXXXXXX@pg-1bb23894-h4tori-5221.g.aivencloud.com:11506/x23?sslmode=require"  # upravte dle Vaší konfigurace
REDIS_URL = "redis://127.0.0.1:6379"

app = FastAPI()
db = Gino()  # inicializace Gino ORM
redis_client: Optional[redis.Redis] = None

# Definice databázového modelu
class Crypto(db.Model):
    __tablename__ = "cryptos"
    id = db.Column(db.Integer(), primary_key=True)
    symbol = db.Column(db.String(), unique=True, nullable=False)
    name = db.Column(db.String())
    metadata = db.Column(db.JSON)  # zde uložíme metadata získaná z API

# Schémata pro validaci vstupních a výstupních dat pomocí Pydantic
class CryptoCreate(BaseModel):
    symbol: str

class CryptoUpdate(BaseModel):
    symbol: Optional[str] = None

class CryptoOut(BaseModel):
    id: int
    symbol: str
    name: str
    metadata: dict

    class Config:
        #orm_mode = True
        from_attributes = True


# Startup a shutdown eventy – připojíme se k DB a Redis a vytvoříme případně tabulky
@app.on_event("startup")
async def startup_event():
    await db.set_bind(DATABASE_URL)
    global redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    await db.gino.create_all()  # vytvoří tabulky podle modelů


@app.on_event("shutdown")
async def shutdown_event():
    global redis_client
    if redis_client is not None:
        await redis_client.close()
    bind = db.pop_bind()
    if bind:
        await bind.close()


# Funkce, která ověří existenci symbolu kryptoměny pomocí Coingecko API
# Využíváme endpoint /search, který hledá dle dotazu a vrací seznam coinů.
# Výsledek ukládáme do Redis cache s TTL např. 1 hodinu.
async def fetch_coin_metadata(symbol: str) -> Optional[dict]:		
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis client není k dispozici")
    key = f"coingecko:{symbol.lower()}"
    cached = await redis_client.get(key)
    #cached=''
    if cached:
        return json.loads(cached)
    url = f"https://api.coingecko.com/api/v3/search?query={symbol}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Chyba při volání Coingecko API")
    print(response)
    
    data = response.json()
    coins = data.get("coins", [])
    # Hledáme coin, u kterého se symbol shoduje (bez ohledu na velikost písmen)
    coin_info = next((coin for coin in coins if coin.get("symbol", "").lower() == symbol.lower()), None)
    if coin_info:
        #print(coin_info)
        await redis_client.set(key, json.dumps(coin_info), ex=3600)
    return coin_info

# Endpoint pro vytvoření nové kryptoměny v DB
# Při vložení ověříme, zda daný symbol existuje na Coingecko.


@app.post("/cryptos", response_model=CryptoOut)
async def create_crypto(crypto: CryptoCreate):
    # Zkontrolovat, zda již daný symbol existuje
    existing = await Crypto.query.where(Crypto.symbol == crypto.symbol.upper()).gino.first()
    if existing:
        raise HTTPException(status_code=409, detail="Kryptoměna se stejným symbolem již existuje")
    metadata = await fetch_coin_metadata(crypto.symbol)
    if not metadata:
        raise HTTPException(status_code=404, detail="Kryptoměna se zadaným symbolem nebyla nalezena na Coingecko")
    name = metadata.get("name")
    new_crypto = await Crypto.create(symbol=crypto.symbol.upper(), name=name, metadata=metadata)
    return new_crypto




# Endpoint pro vypsání všech kryptoměn
@app.get("/cryptos", response_model=List[CryptoOut])
async def list_cryptos():
    cryptos = await Crypto.query.gino.all()
    return cryptos

# Endpoint pro získání detailu jedné kryptoměny dle ID
@app.get("/cryptos/{crypto_id}", response_model=CryptoOut)
async def get_crypto(crypto_id: int):
    crypto = await Crypto.get(crypto_id)
    if not crypto:
        raise HTTPException(status_code=404, detail="Kryptoměna nenalezena")
    return crypto

# Endpoint pro aktualizaci kryptoměny – např. změnit symbol a tím i metadata získaná z Coingecko
@app.put("/cryptos/{crypto_id}", response_model=CryptoOut)
async def update_crypto(crypto_id: int, crypto_data: CryptoUpdate):
    crypto = await Crypto.get(crypto_id)
    if not crypto:
        raise HTTPException(status_code=404, detail="Kryptoměna nenalezena")
    update_data = {}
    if crypto_data.symbol:
        metadata = await fetch_coin_metadata(crypto_data.symbol)
        if not metadata:
            raise HTTPException(status_code=404, detail="Kryptoměna se zadaným symbolem nebyla nalezena na Coingecko")
        update_data["symbol"] = crypto_data.symbol.upper()
        update_data["name"] = metadata.get("name")
        update_data["metadata"] = metadata
    await crypto.update(**update_data).apply()
    return crypto

# Endpoint pro odstranění kryptoměny z DB
@app.delete("/cryptos/{crypto_id}")
async def delete_crypto(crypto_id: int):
    crypto = await Crypto.get(crypto_id)
    if not crypto:
        raise HTTPException(status_code=404, detail="Kryptoměna nenalezena")
    await crypto.delete()
    return {"message": "Kryptoměna byla úspěšně smazána"}

# Endpoint pro "refresh" metadat – aplikace aktualizuje metadata kryptoměny pomocí Coingecko API
@app.post("/cryptos/{crypto_id}/refresh", response_model=CryptoOut)
async def refresh_crypto(crypto_id: int):
    crypto = await Crypto.get(crypto_id)
    if not crypto:
        raise HTTPException(status_code=404, detail="Kryptoměna nenalezena")
    metadata = await fetch_coin_metadata(crypto.symbol)
    if not metadata:
        raise HTTPException(status_code=404, detail="Kryptoměna se zadaným symbolem nebyla nalezena při refreshi")
    await crypto.update(name=metadata.get("name"), metadata=metadata).apply()
    return crypto
