import streamlit as st
import requests
import pandas as pd
import math
import re
from typing import List, Dict, Optional

st.set_page_config(page_title="Cloud Price Compare", layout="wide")

# ---------------- Helper functions ----------------

def safe_json_get(resp):
    """Safely parse JSON from a requests.Response, return None on parse error."""
    try:
        return resp.json()
    except Exception:
        return None

def parse_vcpu_ram_from_meter(meter_name: str):
    """Try to extract 'X vCPU' and 'Y GiB' from meterName / product strings."""
    if not meter_name:
        return None, None
    # Example patterns: "8 vCPU", "32 GiB", "8 vCPU, 32 GiB"
    vcpu = None
    ram = None
    try:
        m = re.search(r'(\d+)\s*vCPU', meter_name, flags=re.IGNORECASE)
        if m:
            vcpu = int(m.group(1))
    except Exception:
        vcpu = None
    try:
        m2 = re.search(r'(\d+)\s*GiB', meter_name, flags=re.IGNORECASE)
        if m2:
            ram = int(m2.group(1))
    except Exception:
        ram = None
    return vcpu, ram


def find_best_match(instances: List[Dict], req_cpu: int, req_ram: int) -> Optional[Dict]:
    """Pick instance with minimal score. Skip records where cpu or ram is missing."""
    best = None
    best_score = float('inf')

    if not instances:
        return None

    for inst in instances:
        cpu = inst.get("vcpu")
        ram = inst.get("memoryGb")

        # skip invalid specs
        if cpu is None or ram is None:
            continue

        try:
            score = abs(float(cpu) - req_cpu) + abs(float(ram) - req_ram)
        except Exception:
            continue

        if score < best_score:
            best_score = score
            best = inst

    return best

# ---------------- Azure fetching ----------------

def fetch_azure(region="centralindia", prefer_inr=True) -> List[Dict]:
    """Fetch Azure retail prices for Virtual Machines (India region). Parse vCPU & RAM when possible."""
    results = []
    base = "https://prices.azure.com/api/retail/prices"
    # limit filter to Virtual Machines in chosen region
    filter_q = f"armRegionName eq '{region}' and serviceName eq 'Virtual Machines'"
    url = base + "?$filter=" + requests.utils.requote_uri(filter_q)

    while url:
        try:
            resp = requests.get(url, timeout=30)
        except Exception as e:
            st.warning(f"Azure request failed: {e}")
            break

        j = safe_json_get(resp)
        if not j:
            st.warning("Azure returned non-JSON or empty response; stopped Azure fetch.")
            break

        items = j.get("Items", [])
        for it in items:
            # try to parse vcpu/ram from meterName (best effort)
            meter = it.get("meterName") or it.get("productName") or ""
            vcpu, ram = parse_vcpu_ram_from_meter(meter)

            # Some Azure records may have 'armSkuName' and other metadata; attempt more parsing
            if vcpu is None or ram is None:
                # Sometimes meterName contains both "8 vCPU" and "32 GiB", try productName too
                vcpu2, ram2 = parse_vcpu_ram_from_meter(it.get("productName", ""))
                if vcpu is None:
                    vcpu = vcpu2
                if ram is None:
                    ram = ram2

            # unitPrice might be present; prefer retailPrice if available
            price = it.get("retailPrice") if it.get("retailPrice") is not None else it.get("unitPrice")

            results.append({
                "csp": "Azure",
                "sku": it.get("armSkuName") or it.get("skuName"),
                "vcpu": vcpu,
                "memoryGb": ram,
                "pricePerHour": (price * 83) if isinstance(price, (int, float)) else None,
                "skuId": it.get("armSkuName") or it.get("skuId") or it.get("skuName"),
                "raw": it
            })

        url = j.get("NextPageLink")
    return results

# ---------------- AWS fetching ----------------

def fetch_aws(region="ap-south-1") -> List[Dict]:
    """Fetch AWS EC2 offer index for specified region (best-effort)."""
    results = []
    offer_name = "AmazonEC2"
    idx_url = f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{offer_name}/current/index.json"

    try:
        idx = requests.get(idx_url, timeout=60).json()
    except Exception as e:
        st.warning(f"AWS offers index fetch failed: {e}")
        return results

    # Attempt to find the products URL or use products in the index
    products = None
    if "products" in idx:
        products = idx["products"]
        terms = idx.get("terms", {})
    else:
        # try to fetch the products JSON if currentVersionUrl present
        try:
            offers = idx.get("offers", {})
            cur = offers.get(offer_name, {}).get("currentVersionUrl")
            if cur:
                products_json_url = cur if cur.startswith("http") else f"https://pricing.us-east-1.amazonaws.com{cur}"
                big = requests.get(products_json_url, timeout=120).json()
                products = big.get("products", {})
                terms = big.get("terms", {})
            else:
                # fallback: nothing workable
                st.warning("AWS offers index lacks 'products' and no currentVersionUrl found.")
                return results
        except Exception as e:
            st.warning(f"Failed to fetch AWS products file: {e}")
            return results

    # iterate products and extract vcpu/memory and basic price for Linux on-demand if present
    od_terms = terms.get("OnDemand", {}) if isinstance(terms, dict) else {}
    for sku, p in products.items():
        attrs = p.get("attributes", {})
        # basic filters
        if attrs.get("servicecode") != "AmazonEC2":
            continue

        inst_type = attrs.get("instanceType") or attrs.get("InstanceType")
        # try parse vcpu and memory
        try:
            vcpu = int(attrs.get("vcpu")) if attrs.get("vcpu") else None
        except:
            vcpu = None
        try:
            # memory sometimes in form "32 GiB"
            mem_raw = attrs.get("memory")
            if mem_raw and isinstance(mem_raw, str):
                mem_val = float(mem_raw.split()[0])
            else:
                mem_val = float(attrs.get("memoryGiB")) if attrs.get("memoryGiB") else None
            ram = mem_val
        except:
            ram = None

        # pick price from on-demand terms for that sku
        unit_price_inr = None
        if sku in od_terms:
            try:
                first_od = next(iter(od_terms[sku].values()))
                # priceDimensions is a dict; pick first, then currency mapping
                pd = next(iter(first_od.get("priceDimensions", {}).values()))
                ppu = pd.get("pricePerUnit", {})
                # prefer INR if present, else USD
                if "INR" in ppu and ppu.get("INR"):
                    unit_price_inr = float(ppu.get("INR"))
                elif "USD" in ppu and ppu.get("USD"):
                    unit_price_inr = float(ppu.get("USD")) * 83
                else:
                    # any currency value present?
                    for cur, val in ppu.items():
                        try:
                            unit_price_inr = float(val) * (83 if cur == "USD" else 1.0)
                            break
                        except:
                            continue
            except Exception:
                unit_price_inr = None

        results.append({
            "csp": "AWS",
            "sku": inst_type,
            "vcpu": vcpu,
            "memoryGb": ram,
            "pricePerHour": unit_price_inr,
            "skuId": sku,
            "raw": p
        })

    return results

# ---------------- GCP fetching ----------------

def fetch_gcp() -> List[Dict]:
    """Fetch public GCP pricelist (best-effort). If non-JSON or fetch fails, return empty list."""
    url = "https://cloudpricingcalculator.appspot.com/static/data/pricelist.json"
    try:
        resp = requests.get(url, timeout=30)
        data = safe_json_get(resp)
        if not data:
            st.warning("GCP pricelist returned non-JSON or empty response. GCP matching will be skipped.")
            return []
    except Exception as e:
        st.warning(f"GCP pricelist fetch error: {e}")
        return []

    results = []
    # Typical structure: top-level keys like 'gcp_price_list' or keys for machine price entries.
    # We'll scan for machine-like keys (heuristic).
    for key, value in data.items():
        # look for known machine prefixes
        if isinstance(key, str) and ("standard" in key.lower() or "n1" in key.lower() or "n2" in key.lower() or key.startswith("CP-COMPUTEENGINE-VMIMAGE")):
            # try to extract machine name and price (IN field if present)
            sku = key
            # price may be nested or be numeric
            price_usd = None
            price_in = None
            if isinstance(value, dict):
                # prefer India region 'IN' if present
                price_in = value.get("IN") or value.get("india")
                # fallback to 'us' or 'USD' keys
                price_usd = value.get("US") or value.get("usd") or value.get("price")
            else:
                # non-dict value
                try:
                    price_usd = float(value)
                except:
                    price_usd = None

            # Heuristic to guess vcpu/ram from string: try to find '-<n>' suffix indicating vCPU
            vcpu = None
            ram = None
            m = re.search(r'(\d+)(?:$|-)', key)
            if m:
                try:
                    vcpu = int(m.group(1))
                    # common GCP n1-standard has 3.75GB per vCPU
                    ram = round(vcpu * 3.75, 2)
                except:
                    vcpu = None

            unit_price_inr = None
            try:
                if price_in:
                    unit_price_inr = float(price_in)  # sometimes already INR
                elif price_usd:
                    unit_price_inr = float(price_usd) * 83
            except Exception:
                unit_price_inr = None

            results.append({
                "csp": "GCP",
                "sku": sku,
                "vcpu": vcpu,
                "memoryGb": ram,
                "pricePerHour": unit_price_inr,
                "skuId": sku,
                "raw": value
            })
    return results

# ---------------- Streamlit UI ----------------

st.title("☁ Cloud Price Comparison Tool")
st.write("Compare VM prices across Azure, AWS and GCP (India region).")

csp_selection = st.multiselect("Select Cloud Providers", ["Azure", "AWS", "GCP"], default=["Azure", "AWS", "GCP"])
vCPU = st.slider("vCPU", min_value=1, max_value=128, value=8)
RAM = st.slider("RAM (GB)", min_value=1, max_value=1024, value=32)

if st.button("Compare Pricing"):
    status_msgs = []
    results = []

    if "Azure" in csp_selection:
        st.info("Fetching Azure catalog (may take ~10-30s)...")
        az_list = fetch_azure()
        st.write(f"Azure entries fetched: {len(az_list)}")
        match = find_best_match(az_list, vCPU, RAM)
        if match:
            results.append(match)
            status_msgs.append("Azure: matched SKU " + str(match.get("sku")))
        else:
            status_msgs.append("Azure: no match found (Azure data often lacks vCPU/RAM in retail API).")

    if "AWS" in csp_selection:
        st.info("Fetching AWS catalog (may take ~20-40s)...")
        aws_list = fetch_aws()
        st.write(f"AWS entries fetched: {len(aws_list)}")
        match = find_best_match(aws_list, vCPU, RAM)
        if match:
            results.append(match)
            status_msgs.append("AWS: matched SKU " + str(match.get("sku")))
        else:
            status_msgs.append("AWS: no match found.")

    if "GCP" in csp_selection:
        st.info("Fetching GCP pricelist (best-effort)...")
        gcp_list = fetch_gcp()
        st.write(f"GCP entries parsed: {len(gcp_list)}")
        match = find_best_match(gcp_list, vCPU, RAM)
        if match:
            results.append(match)
            status_msgs.append("GCP: matched SKU " + str(match.get("sku")))
        else:
            status_msgs.append("GCP:_
