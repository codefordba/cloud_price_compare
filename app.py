# cloud_price_compare/app.py
import streamlit as st
import requests
import pandas as pd
import math
import json
import os
from typing import List, Dict, Optional

st.set_page_config(page_title="Cloud Price Compare (Top-5)", layout="wide")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
AWS_CATALOG_PATH = os.path.join(DATA_DIR, "aws_catalog.json")
AZURE_CATALOG_PATH = os.path.join(DATA_DIR, "azure_catalog.json")

# ---------- Utilities ----------
def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def score_distance(req_cpu: int, req_ram: float, vcpu: int, ram: float) -> float:
    # Weighted distance: CPU difference + RAM difference / 4 (to normalize)
    return abs(vcpu - req_cpu) + (abs(ram - req_ram) / 4.0)

def top_n_matches(items: List[Dict], req_cpu: int, req_ram: float, n: int = 5):
    scored = []
    for it in items:
        v = it.get("vcpu")
        r = it.get("memoryGb")
        if v is None or r is None:
            continue
        sc = score_distance(req_cpu, req_ram, v, r)
        scored.append((sc, it))
    scored.sort(key=lambda x: x[0])
    return [itm for _, itm in scored[:n]]

# ---------- Price lookup helpers ----------
AZURE_RETAIL_BASE = "https://prices.azure.com/api/retail/prices"

def fetch_azure_price_for_sku(arm_sku_name: str, region: str = "centralindia", prefer_currency: str = "INR") -> Optional[Dict]:
    if not arm_sku_name:
        return None
    filter_q = f"armRegionName eq '{region}' and armSkuName eq '{arm_sku_name}'"
    url = AZURE_RETAIL_BASE + "?$filter=" + requests.utils.requote_uri(filter_q)
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        j = r.json()
        items = j.get("Items", [])
        if not items:
            return None
        # prefer currency
        for it in items:
            cur = it.get("currencyCode")
            price = it.get("retailPrice") if it.get("retailPrice") is not None else it.get("unitPrice")
            if prefer_currency and cur == prefer_currency and price is not None:
                return {"unitPrice": price, "currency": cur, "raw": it}
        # fallback
        for it in items:
            price = it.get("retailPrice") if it.get("retailPrice") is not None else it.get("unitPrice")
            if price is not None:
                return {"unitPrice": price, "currency": it.get("currencyCode"), "raw": it}
    except Exception:
        return None
    return None

# For AWS: we will NOT download the full offers file each run.
# Instead this helper will attempt a minimal on-demand fetch by product SKU if possible.
# NOTE: This is best-effort; availability of direct SKU lookup depends on AWS offer server behavior.
AWS_OFFERS_BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current"

def fetch_aws_price_for_sku(product_sku: str, prefer_currency: str = "INR") -> Optional[Dict]:
    """
    Try to fetch price details for a given AWS product SKU by querying the offers endpoint for that SKU's terms.
    This function tries to avoid downloading the full 'products' object. It may fail if offers endpoint doesn't support direct SKU fetch.
    """
    if not product_sku:
        return None

    # Best-effort path: try to fetch a small URL that sometimes holds product/terms for given SKU.
    # If not available, return None (the UI will show the SKU with no live price).
    try:
        # Attempt: fetch the index and look for product's onDemand terms only (lightweight attempt)
        idx_url = AWS_OFFERS_BASE + "/index.json"
        r = requests.get(idx_url, timeout=20)
        r.raise_for_status()
        idx = r.json()
        # Many indexes embed the 'offers' data directly; if not, fallback and return None
        products_obj = idx.get("products")
        terms_obj = idx.get("terms")
        if products_obj and product_sku in products_obj:
            # try to read term price
            terms = terms_obj.get("OnDemand", {})
            if product_sku in terms:
                sku_terms = terms[product_sku]
                # take first price dimension found:
                for od_key, od_val in sku_terms.items():
                    for pd_k, pd in od_val.get("priceDimensions", {}).items():
                        ppu = pd.get("pricePerUnit", {})
                        # prefer INR else USD
                        if prefer_currency and prefer_currency in ppu and ppu.get(prefer_currency):
                            return {"unitPrice": float(ppu.get(prefer_currency)), "currency": prefer_currency}
                        # fallback USD
                        if "USD" in ppu and ppu.get("USD"):
                            return {"unitPrice": float(ppu.get("USD")) * 83.0, "currency": "INR"}
        # else no direct price found
        return None
    except Exception:
        return None

# ---------- Load local catalogs ----------
def load_catalogs():
    aws = []
    azure = []
    try:
        aws = load_json(AWS_CATALOG_PATH)
    except Exception:
        st.warning("Failed to load local AWS catalog (data/aws_catalog.json). AWS matching disabled.")
    try:
        azure = load_json(AZURE_CATALOG_PATH)
    except Exception:
        st.warning("Failed to load local Azure catalog (data/azure_catalog.json). Azure matching disabled.")
    return aws, azure

# ---------- Streamlit UI ----------
st.title("☁ Cloud VM Price Compare — Top 5 matches (India)")

col1, col2 = st.columns([3,1])
with col1:
    req_vcpu = st.number_input("Required vCPU", min_value=1, max_value=128, value=8, step=1)
    req_ram = st.number_input("Required RAM (GB)", min_value=1, max_value=2048, value=32, step=1)
    providers = st.multiselect("Providers", ["Azure", "AWS"], default=["Azure","AWS"])
    top_n = st.slider("Top N matches per provider", min_value=1, max_value=10, value=5)
    fetch_live_prices = st.checkbox("Attempt live price lookup for matched SKUs (Azure fast, AWS best-effort)", value=True)
with col2:
    st.markdown("**Region (Azure live prices)**: centralindia")
    st.markdown("**AWS price conversion**: USD→INR using 83.0 where needed")
    st.markdown("**Catalog**: local SKU catalogs are used for instant matching (edit JSONs in /data to extend)**")

if st.button("Compare"):
    st.info("Matching SKUs and (optionally) fetching live prices. This runs fast.")
    aws_catalog, azure_catalog = load_catalogs()

    all_results = []

    # Azure
    if "Azure" in providers and azure_catalog:
        azure_matches = top_n_matches(azure_catalog, req_vcpu, req_ram, n=top_n)
        for m in azure_matches:
            price_entry = None
            if fetch_live_prices:
                price_entry = fetch_azure_price_for_sku(m["name"], region="centralindia", prefer_currency="INR")
            all_results.append({
                "csp": "Azure",
                "sku": m["name"],
                "vcpu": m["vcpu"],
                "memoryGb": m["memoryGb"],
                "series": m.get("series"),
                "pricePerHour_INR": round(price_entry["unitPrice"],4) if price_entry else None,
                "priceCurrency": price_entry["currency"] if price_entry else None,
                "skuId": m["name"]
            })

    # AWS
    if "AWS" in providers and aws_catalog:
        aws_matches = top_n_matches(aws_catalog, req_vcpu, req_ram, n=top_n)
        for m in aws_matches:
            live_price = None
            if fetch_live_prices:
                # Attempt best-effort small lookup
                live_price = fetch_aws_price_for_sku(m.get("skuId") or m.get("sku"))
            all_results.append({
                "csp": "AWS",
                "sku": m.get("sku"),
                "vcpu": m.get("vcpu"),
                "memoryGb": m.get("memoryGb"),
                "series": m.get("family"),
                "pricePerHour_INR": round(live_price["unitPrice"],4) if live_price else (m.get("pricePerHour_INR") if m.get("pricePerHour_INR") is not None else None),
                "priceCurrency": live_price["currency"] if live_price else ("INR" if m.get("pricePerHour_INR')") else None),
                "skuId": m.get("skuId") or m.get("sku")
            })

    # Present results
    if not all_results:
        st.warning("No matches found (maybe catalogs are missing). Check /data/*.json")
    else:
        df = pd.DataFrame(all_results)
        # compute month cost if price exists
        df["pricePerMonth_INR"] = df["pricePerHour_INR"].apply(lambda x: round(x*24*30,2) if x is not None else None)
        st.dataframe(df[["csp","sku","series","vcpu","memoryGb","pricePerHour_INR","pricePerMonth_INR","priceCurrency","skuId"]])

        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", csv, "csp_vm_top_matches.csv", "text/csv")
    st.success("Done.")
