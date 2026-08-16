"""
eSSL / ZKTeco Biometric Device Simulator (Python)
Use this to test your FastAPI iClock server without needing the physical device.
"""
import sys
import urllib.request
import urllib.parse
from datetime import datetime

PORT = 8000
SN = "NFZ8254900401"
BASE_URL = f"http://localhost:{PORT}"

def run_simulation():
    print("==================================================")
    print(" 🧪 Starting eSSL Biometric Device Push Simulation")
    print("==================================================")

    # 1. Handshake
    print("\n1. Testing Device Handshake (GET /iclock/cdata)...")
    url = f"{BASE_URL}/iclock/cdata?SN={SN}&options=all&pushver=3.2.1"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            print(f"   HTTP Status: {resp.status}")
            print(f"   Response:\n{body}")
            assert "GET OPTION FROM" in body
            assert "Stamp=0" in body
            print("   ✅ Handshake successful (Stamp=0 verified)!")
    except Exception as e:
        print(f"   ❌ Handshake failed: {e}")
        return

    # 2. Real Attendance Push (POST /iclock/cdata?table=ATTLOG)
    print("\n2. Testing Standard Attendance Push (POST /iclock/cdata?table=ATTLOG)...")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    punch_data = f"1\t{now_str}\t0\t1\t0\t\t\t0\t0\t\n101\t{now_str}\t1\t1\t0\t0\n"
    url = f"{BASE_URL}/iclock/cdata?SN={SN}&table=ATTLOG&Stamp=1"
    try:
        req = urllib.request.Request(url, data=punch_data.encode("utf-8"), headers={"Content-Type": "text/plain"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            print(f"   HTTP Status: {resp.status}")
            print(f"   Response: {body}")
            assert body.strip() == "OK"
            print("   ✅ Standard attendance push successful!")
    except Exception as e:
        print(f"   ❌ Attendance push failed: {e}")
        return

    # 3. Real-Time Push (POST /iclock/cdata?table=rtlog)
    print("\n3. Testing Real-time Push (POST /iclock/cdata?table=rtlog)...")
    rt_punch_data = f"111\t{now_str}\t0\t1\t0\n"
    url = f"{BASE_URL}/iclock/cdata?SN={SN}&table=rtlog"
    try:
        req = urllib.request.Request(url, data=rt_punch_data.encode("utf-8"), headers={"Content-Type": "text/plain"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            print(f"   HTTP Status: {resp.status}")
            print(f"   Response: {body}")
            assert body.strip() == "OK"
            print("   ✅ Real-time table=rtlog push successful!")
    except Exception as e:
        print(f"   ❌ Real-time push failed: {e}")
        return

    # 4. Direct Real-Time Route (POST /iclock/rtlog)
    print("\n4. Testing Direct Endpoint (POST /iclock/rtlog)...")
    url = f"{BASE_URL}/iclock/rtlog?SN={SN}"
    try:
        req = urllib.request.Request(url, data=rt_punch_data.encode("utf-8"), headers={"Content-Type": "text/plain"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            print(f"   HTTP Status: {resp.status}")
            print(f"   Response: {body}")
            assert body.strip() == "OK"
            print("   ✅ Direct /iclock/rtlog push successful!")
    except Exception as e:
        print(f"   ❌ Direct rtlog push failed: {e}")
        return

    # 5. Command Polling
    print("\n5. Testing Command Polling (GET /iclock/getrequest)...")
    url = f"{BASE_URL}/iclock/getrequest?SN={SN}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            print(f"   HTTP Status: {resp.status}")
            print(f"   Response: {body}")
            print("   ✅ Command poll successful!")
    except Exception as e:
        print(f"   ❌ Command poll failed: {e}")
        return

    print("\n==================================================")
    print(f" 🎉 All device simulation tests completed successfully!")
    print(f" Open http://localhost:{PORT} in your browser to view the live dashboard.")
    print("==================================================\n")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])
        BASE_URL = f"http://localhost:{PORT}"
    run_simulation()
