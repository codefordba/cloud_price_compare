import streamlit as st
import requests
import pandas as pd
import math

st.set_page_config(page_title="Cloud Price Compare", layout="wide")

# --------------------------------- Matching ---------------------------------
def find_best_match(instances, req_cpu, req_ram):
    best = None
    best_score = float("inf")

    for inst in instances:
        cpu = inst.get("vcpu")
        ram = inst.get("memoryGb")

        if cpu is None or ram is None:
            continue

        score = abs(cpu - req_cpu) + abs(ram - req_ram)
        if score < best_score:
            best_score = score
            best = inst

    return best

# --------------------------------- Azure API ---------------------------------
def fetch_azure():
    url = "https://prices.azure.com/api/retail/prices?$filter=serviceName eq 'Virtual Machines' and armRegionName eq 'centralindia'"
    results = []
    while url:
        r = requests.get(url)
        j = r.json()
        for item in j.get("Items", []):
            price = item.get("retailPrice")
            if price and item.get("armSkuName"):
                results.append({
                    "csp": "Azure",
                    "sku": item["armSkuName"],
                    "vcpu": item.get("cores"),
                    "memoryGb": item.get("ram"),
                    "pricePerHour": price * 83,
                    "skuId": item.get("armSkuName")
                })
        url = j.get("NextPageLink")
    return results

# --------------------------------- AWS API ---------------------------------
def fetch_aws():
    url = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/ap-south-1/index.json"
    data = requests.get(url).json()
    results = []

    for sku, product in data["products"].items():
        attrs = product.get("attributes", {})
        if attrs.get("servicecode") != "AmazonEC2":
            continue

        inst_type = attrs.get("instanceType")
        vcpu = attrs.get("vcpu")
        mem = attrs.get("memory")

        if not inst_type or not vcpu or not mem:
            continue

        try:
            ram = float(mem.split(" ")[0])
            vcpu = int(vcpu)
        except:
            continue

        price_item = data["terms"]["OnDemand"].get(sku, {})
        for _, val in price_item.items():
            priceusd = list(val["priceDimensions"].values())[0]["pricePerUnit"]["USD"]
            price_inr = float(priceusd) * 83
            results.append({
                "csp": "AWS",
                "sku": inst_type,
                "vcpu": vcpu,
                "memoryGb": ram,
                "pricePerHour": price_inr,
                "skuId": sku
            })
    return results

# --------------------------------- UI ---------------------------------
st.title("☁ Cloud Price Comparison Tool")
st.write("Compare VM Pricing – Azure vs AWS – India Region (INR)")

csp = st.multiselect("Select Cloud Providers", ["Azure", "AWS"], default=["Azure", "AWS"])
vcpu = st.slider("vCPU", 1, 64, 8)
ram = st.slider("RAM (GB)", 1, 256, 32)

if st.button("Compare Pricing"):
    results = []

    if "Azure" in csp:
        st.info("Fetching Azure Pricing...")
        az = fetch_azure()
        results.append(find_best_match(az, vcpu, ram))

    if "AWS" in csp:
        st.info("Fetching AWS Pricing...")
        aws = fetch_aws()
        results.append(find_best_match(aws, vcpu, ram))

    output = [r for r in results if r is not None]

    if not output:
        st.error("❌ No matching SKU found. Increase tolerance or change filters.")
    else:
        df = pd.DataFrame(output)
        df["pricePerMonth"] = df["pricePerHour"] * 24 * 30
        st.dataframe(df)

        st.download_button(
            "📥 Download CSV",
            df.to_csv(index=False).encode("utf-8"),
            "cloud-price-compare.csv",
            "text/csv"
        )
