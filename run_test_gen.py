import sys
import time

sys.path.insert(0, r"C:\Users\AbolUp\Downloads\flowbridge")
from fastapi.testclient import TestClient
from bridge import app

prompt = "a cute baby red panda sitting on a mossy stone in a lush bamboo forest, highly detailed cinematic lighting"

print("Starting real image generation test...")
print(f"Prompt: {prompt}")

t0 = time.time()
with TestClient(app) as client:
    print("Client ready, sending POST /generate...")
    req_start = time.time()
    resp = client.post("/generate", json={"prompt": prompt})
    req_time = time.time() - req_start

total_time = time.time() - t0

print(f"Status code: {resp.status_code}")
if resp.status_code == 200:
    out_path = r"C:\Users\AbolUp\Downloads\generated_test.jpg"
    with open(out_path, "wb") as f:
        f.write(resp.content)
    server_ms = resp.headers.get("x-flowbridge-ms", "N/A")
    model_name = resp.headers.get("x-flowbridge-model", "N/A")
    print(f"SUCCESS: Image saved to {out_path} ({len(resp.content):,} bytes)")
    print(f"Model used: {model_name}")
    print(f"Server-reported time (X-Flowbridge-Ms): {server_ms} ms")
    print(f"Total round-trip time: {req_time:.2f} seconds")
else:
    print(f"FAILED: {resp.status_code} - {resp.text}")
