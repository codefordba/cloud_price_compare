import streamlit as st
import pandas as pd
import requests
import math

st.set_page_config(page_title="Cloud Price Compare", layout="wide")

# ========= Safe Matching Function =========
def find_best_match(instances, req_cpu, req_ram):
    best = None
    best_score = math.inf

    for inst in instances:
        cpu = inst.get("vcpu")
        ram = inst.get("memoryGb")

        if cpu is None or ram is None:
            continue  # skip invalid rows

        score = abs(cpu - req_cpu) + abs(ram - req_ram)

        if score < best_score:
            best_score = score
            best = inst

    return best

# ========= AWS Pricing =========
def fetch_aws():
    url = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/index.json"
    data = requests.get(url).json()
    results = []

    for sku, meta in data["products"].items():
        if meta.get("productFamily") != "Compute Instance":
            continue

        attributes = meta.get("attributes", {})
        instance_type = attributes.get("instanceType")
        cpu = attributes.get("vcpu")
        ram = attributes.get("memory")

        if not cpu or not ram or not instance_type:
            continue

        ram_gb = float(ram.replace(" GiB", "").strip())

        pricing = data["terms"]["OnDemand"].get(sku, {})
        for _, offer in pricing.items():
            price = list(offer["priceDimensions"].values())[0]["pricePerUnit"]["USD"]
            price_inr = float(price) * 83

            results.append({
                "csp": "AWS",
                "sku": instance_type,
                "vcpu": int(cpu),
                "memoryGb": ram_gb,
                "pricePerHour": price_inr
            })

    return results

# ========= Azure Pricing (Retail API) =========
def fetch_azure():
    url = "https://prices.azure.com/api/retail/prices?$filter=armRegionName eq 'centralindia' and serviceName eq 'Virtual Machines'"
    results = []

    while url:
        data = requests.get(url).json()
        for item in data.get("Items", []):
            sku = item.get("armSkuName")

            cpu = item.get("cores")
            ram = item.get("ram")

            if cpu is None or ram is None or sku is None:
                continue

            results.append({
                "csp": "Azure",
                "sku": sku,
                "vcpu": cpu,
                "memoryGb": ram,
                "pricePerHour": item.get("retailPrice", 0) * 83
            })

        url = data.get("NextPageLink")

    return results

# ========= UI =========
st.header("☁ Cloud VM Price Comparison Tool")

vCPU = st.number_input("Select vCPU", 1, 256, 4)
RAM = st.number_input("Select RAM (GB)", 1, 2048, 16)
csp_selection = st.multiselect("Select CSP", ["AWS", "Azure"])

if st.button("Compare Pricing"):
    with st.spinner("Fetching and processing..."):

        results = []

        if "Azure" in csp_selection:
            results.append(find_best_match(fetch_azure(), vCPU, RAM))

        if "AWS" in csp_selection:
            results.append(find_best_match(fetch_aws(), vCPU, RAM))

        df = pd.DataFrame([r for r in results if r])
        st.dataframe(df)

        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8"),
            "vm_comparison.csv",
            "text/csv"
        )
