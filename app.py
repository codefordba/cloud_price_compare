import streamlit as st
import requests
import pandas as pd
import math

# ---------------- Helper Functions ----------------
def find_best_match(instances, req_cpu, req_ram):
    best = None
    best_score = math.inf

    for inst in instances:
        cpu = inst.get("vcpu")
        ram = inst.get("memoryGb")

        # skip records where CPU or RAM is missing
        if cpu is None or ram is None:
            continue

        score = abs(cpu - req_cpu) + abs(ram - req_ram)

        if score < best_score:
            best_score = score
            best = inst

    return best


# ---------------- Fetch Azure Pricing ----------------
def fetch_azure():
    url = "https://prices.azure.com/api/retail/prices?$filter=armRegionName eq 'centralindia' and serviceName eq 'Virtual Machines'"
    results = []
    while url:
        data = requests.get(url).json()
        for item in data.get("Items", []):
            if item.get("unitPrice", 0) > 0:
                results.append({
                    "csp": "Azure",
                    "sku": item.get("armSkuName"),
                    "vcpu": item.get("cores"),            # may be None
                    "memoryGb": item.get("ram"),          # may be None
                    "pricePerHour": item["unitPrice"] * 83,
                    "skuId": item.get("armSkuName")
                })
        url = data.get("NextPageLink")
    return results


# ---------------- Fetch AWS Pricing ----------------
def fetch_aws():
    url = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/ap-south-1/index.json"
    data = requests.get(url).json()
    results = []
    for sku, product in data["products"].items():
        attrs = product.get("attributes", {})
        if attrs.get("servicecode") == "AmazonEC2" and attrs.get("operatingSystem") == "Linux":
            try:
                vcpu = int(attrs.get("vcpu"))
                ram = float(attrs.get("memory").split(" ")[0])
                instance_type = attrs.get("instanceType")
            except:
                continue

            price_terms = data["terms"]["OnDemand"].get(sku, {})
            for _, val in price_terms.items():
                price = float(list(val["priceDimensions"].values())[0]["pricePerUnit"]["USD"])
                results.append({
                    "csp": "AWS",
                    "sku": instance_type,
                    "vcpu": vcpu,
                    "memoryGb": ram,
                    "pricePerHour": price * 83,
                    "skuId": sku
                })
    return results

# ---------------- Fetch GCP Pricing ----------------
def fetch_gcp():
    url = "https://cloudpricingcalculator.appspot.com/static/data/pricelist.json"
    data = requests.get(url).json()
    results = []

    for key, val in data.items():
        if key.startswith("CP-COMPUTEENGINE-VMIMAGE"):
            results.append({
                "csp": "GCP",
                "sku": key.split("-")[-1],
                "vcpu": 8,         # generic default (optional : enhance next)
                "memoryGb": 32,
                "pricePerHour": val.get("IN", 0),
                "skuId": key
            })
    return results

# ---------------- Streamlit UI ----------------
st.title("☁ Cloud Price Comparison Tool")
st.write("Compare VM Prices across Azure, AWS, and GCP (India Region, INR)")

csp_selection = st.multiselect("Select Cloud Providers", ["Azure", "AWS", "GCP"], default=["Azure", "AWS", "GCP"])
vCPU = st.slider("Select vCPU", 2, 64, 8)
RAM = st.slider("Select RAM (GB)", 2, 256, 32)

if st.button("Compare Pricing"):
    results = []

    if "Azure" in csp_selection:
        az = fetch_azure()
        results.append(find_best_match(az, vCPU, RAM))

    if "AWS" in csp_selection:
        aws = fetch_aws()
        results.append(find_best_match(aws, vCPU, RAM))

    if "GCP" in csp_selection:
        gcp = fetch_gcp()
        results.append(find_best_match(gcp, vCPU, RAM))

    df = pd.DataFrame(results)
    df["pricePerMonth"] = df["pricePerHour"] * 24 * 30

    st.dataframe(df)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, "comparison_output.csv", "text/csv")
