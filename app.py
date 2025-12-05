# cloud_price_compare/app.py
import streamlit as st
import requests
import pandas as pd
import math
import time
from typing import List, Dict, Optional

st.set_page_config(page_title="Cloud Price Compare", layout="wide")

# ---------------------------
# Embedded small Azure SKU catalog (starter)
# - You can expand this list by adding more SKUs later to data file.
# - Each entry: name (armSkuName), vcpu, memoryGb, series
# ---------------------------
AZURE_SKUS = [
    {"name": "Standard_D2s_v5", "vcpu": 2, "memoryGb": 8, "series": "Dsv5"},
    {"name": "Standard_D4s_v5", "vcpu": 4, "memoryGb": 16, "series": "Dsv5"},
    {"name": "Standard_D8s_v5", "vcpu": 8, "memoryGb": 32, "series": "Dsv5"},
    {"name": "Standard_D16s_v5", "vcpu": 16, "memoryGb": 64, "series": "Dsv5"},
    {"name": "Standard_D32s_v5", "vcpu": 32, "memoryGb": 128, "series": "Dsv5"},
    {"name": "Standard_E2s_v5", "vcpu": 2, "memoryGb": 16, "series": "Esv5"},
    {"name": "Standard_E4s_v5", "vcpu": 4, "memoryGb": 32, "series": "Esv5"},
    {"name": "Standard_E8s_v5", "vcpu": 8, "memoryGb": 64, "series": "Esv5"},
    {"name": "Standard_E16s_v5", "vcpu": 16, "memoryGb": 128, "series": "Esv5"},
    {"name": "Standard_F4s_v2", "vcpu": 4, "memoryGb": 8, "series": "Fsv2"},
    {"name": "Standard_F8s_v2", "vcpu": 8, "memoryGb": 16, "series": "Fsv2"},
    {"name": "Standard_B2s", "vcpu": 2, "memoryGb": 8, "series": "B"},
    {"name": "Standard_B4ms", "vcpu": 4, "memoryGb": 16, "series": "B"},
    {"name": "Standard_NC6", "vcpu": 6, "memoryGb": 56, "series": "NC (GPU)"},
    {"name": "Standard_D64s_v5", "vcpu": 64, "memoryGb": 256, "series": "Dsv5"},
    # add more as you want...
]

# ---------------------------
# Helpers
# ---------------------------
def score_distance(vcpu_req: int, ram_req: int, vcpu: int, ram: float) -> float:
    """Simple distance metric (lower is better)."""
    return abs(vcpu - vcpu_req) + abs(ram - ram_req) / 4.0  # weight RAM a bit less

def top_n_matches_from_catalog(catalog: List[Dict], vcpu_req: int, ram_req: int, n: int = 5) -> List[Dict]:
    scored = []
    for s in catalog:
        if s.get("vcpu") is None or s.get("memoryGb") is None:
            continue
        sc = score_distance(vcpu_req, ram_req, s["vcpu"], s["memoryGb"])
        scored.append((sc, s))
    scored.sort(key=lambda x: x[0])
    return [s for _, s in scored[:n]]

# ---------------------------
# Azure: price per SKU (fast)
# ---------------------------
AZURE_RETAIL_BASE = "https://prices.azure.com/api/retail/prices"

def fetch_azure_price_for_sku(arm_sku_name: str, region: str = "centralindia", prefer_currency: str = "INR") -> Optional[Dict]:
    """
    Fetch price for a single ARM SKU in a region using Retail Prices API.
    Returns {unitPrice, currency, raw} or None.
    """
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
        # prefer INR if present
        for it in items:
            cur = it.get("currencyCode")
            price = it.get("retailPrice") if it.get("retailPrice") is not None else it.get("unitPrice")
            if prefer_currency and cur == prefer_currency and price is not None:
                return {"unitPrice": price, "currency": cur, "raw": it}
        # fallback to first priced item
        for it in items:
            price = it.get("retailPrice") if it.get("retailPrice") is not None else it.get("unitPrice")
            if price is not None:
                return {"unitPrice": price, "currency": it.get("currencyCode"), "raw": it}
    except Exception as e:
        return None
    return None

# ---------------------------
# AWS: fetch product list (filter products with numeric vcpu & memory)
# ---------------------------
AWS_OFFERS_BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/index.json"

def fetch_aws_products_cached() -> Optional[Dict]:
    """Fetch AWS products JSON index (may be large) and return 'products' & 'terms' dicts."""
    try:
        r = requests.get(AWS_OFFERS_BASE, timeout=60)
        r.raise_for_status()
        j = r.json()
        products = j.get("products", {})
        terms = j.get("terms", {})
        return {"products": products, "terms": terms}
    except Exception:
        return None

def aws_extract_instances(products: Dict, terms: Dict) -> List[Dict]:
    """Return list of instances with vcpu, memoryGb and SKU info and on-demand price in INR (if available)."""
    out = []
    od_terms = terms.get("OnDemand", {})
    for sku, p in products.items():
        attrs = p.get("attributes", {})
        if attrs.get("servicecode") != "AmazonEC2":
            continue
        inst_type = attrs.get("instanceType")
        vcpu = attrs.get("vcpu")
        mem = attrs.get("memory")
        if not inst_type or not vcpu or not mem:
            continue
        # parse vcpu and memory
        try:
            vcpu_i = int(float(vcpu))
        except:
            continue
        try:
            mem_gb = float(str(mem).split()[0])
        except:
            continue

        # find price in terms dict
        price_inr = None
        if sku in od_terms:
            try:
                first_od = next(iter(od_terms[sku].values()))
                pd = next(iter(first_od.get("priceDimensions", {}).values()))
                price_per_unit = pd.get("pricePerUnit", {})
                # prefer INR
                if "INR" in price_per_unit and price_per_unit.get("INR"):
                    price_inr = float(price_per_unit.get("INR"))
                elif "USD" in price_per_unit and price_per_unit.get("USD"):
                    price_inr = float(price_per_unit.get("USD")) * 83.0
                else:
                    # take first numeric
                    for cur, val in price_per_unit.items():
                        try:
                            price_inr = float(val) * (83.0 if cur == "USD" else 1.0)
                            break
                        except:
                            continue
            except Exception:
                price_inr = None

        out.append({
            "csp": "AWS",
            "sku": inst_type,
            "vcpu": vcpu_i,
            "memoryGb": mem_gb,
            "pricePerHour": price_inr,
            "skuId": sku
        })
    return out

# ---------------------------
# Utility: top-n matches + add price
# ---------------------------
def enrich_with_prices_and_format(matches: List[Dict], csp: str, region: str = "centralindia") -> List[Dict]:
    rows = []
    if csp == "Azure":
        for m in matches:
            price_entry = fetch_azure_price_for_sku(m["name"], region)
            price = price_entry["unitPrice"] if price_entry else None
            currency = price_entry["currency"] if price_entry else None
            rows.append({
                "csp": "Azure",
                "sku": m["name"],
                "vcpu": m["vcpu"],
                "memoryGb": m["memoryGb"],
                "pricePerHour_INR": (price * 1.0) if price is not None else None,  # Azure retail already region currency (often INR)
                "priceCurrency": currency,
                "skuId": m["name"]
            })
    elif csp == "AWS":
        for m in matches:
            rows.append({
                "csp": "AWS",
                "sku": m["sku"],
                "vcpu": m["vcpu"],
                "memoryGb": m["memoryGb"],
                "pricePerHour_INR": m.get("pricePerHour"),
                "priceCurrency": "INR" if m.get("pricePerHour") is not None else None,
                "skuId": m.get("skuId")
            })
    return rows

# ---------------------------
# Streamlit UI
# ---------------------------
st.title("☁ Cloud VM Price Compare — Top 5 Matches (India)")

col1, col2 = st.columns([2, 1])
with col1:
    vcpu = st.number_input("Required vCPU", min_value=1, max_value=128, value=8, step=1)
    ram = st.number_input("Required RAM (GB)", min_value=1, max_value=2048, value=32, step=1)
    providers = st.multiselect("Select providers", ["Azure", "AWS"], default=["Azure", "AWS"])
    top_n = st.slider("Top N matches per provider", 1, 10, 5)
with col2:
    st.markdown("**Region**: centralindia (Azure retail price calls use this region)")
    st.markdown("**Currency**: INR preferred (conversion used for USD → INR at 83.0 rate)")

if st.button("Compare"):
    st.info("Matching SKUs and fetching prices (fast) — this should take a few seconds per provider...")

    final_rows = []

    # Azure: use embedded catalog for matching (fast), then fetch price per SKU
    if "Azure" in providers:
        az_matches = top_n_matches_from_catalog(AZURE_SKUS, vcpu, ram, n=top_n)
        st.write(f"Azure: found {len(az_matches)} candidate SKUs")
        az_enriched = enrich_with_prices_and_format(az_matches, "Azure", region="centralindia")
        final_rows.extend(az_enriched)

    # AWS: fetch products index, filter and compute top matches (may take ~10-25s first time)
    if "AWS" in providers:
        st.write("AWS: fetching catalog (this may take ~10-30s on first run)...")
        aws_data = fetch_aws_products_cached()
        if aws_data is None:
            st.error("Failed to fetch AWS pricing catalog.")
        else:
            products = aws_data["products"]
            terms = aws_data["terms"]
            aws_instances = aws_extract_instances(products, terms)
            st.write(f"AWS: {len(aws_instances)} instance SKUs parsed (with vCPU & memory).")
            # compute top-n matches
            scored = []
            for inst in aws_instances:
                sc = score_distance(vcpu, ram, inst["vcpu"], inst["memoryGb"])
                scored.append((sc, inst))
            scored.sort(key=lambda x: x[0])
            aws_top = [inst for _, inst in scored[:top_n]]
            aws_enriched = enrich_with_prices_and_format(aws_top, "AWS")
            final_rows.extend(aws_enriched)

    if not final_rows:
        st.warning("No results found for the selected providers/specs.")
    else:
        df = pd.DataFrame(final_rows)
        # compute monthly price (approx)
        df["pricePerMonth_INR"] = df["pricePerHour_INR"].apply(lambda x: round(x * 24 * 30, 2) if x is not None else None)
        st.dataframe(df[["csp", "sku", "vcpu", "memoryGb", "pricePerHour_INR", "pricePerMonth_INR", "priceCurrency", "skuId"]])

        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", csv, "csp_vm_top_matches.csv", "text/csv")

    st.success("Done.")
