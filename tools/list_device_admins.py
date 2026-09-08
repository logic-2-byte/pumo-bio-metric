"""
List the admin fingers enrolled on a reader. RUN THIS ON SITE, BY HAND.

It dials the device directly over TCP 4370, which only works from a machine on
the same branch network as the reader. It will NOT work from the server: a
reader sits behind a branch router with no route back in, which is precisely
why the service itself no longer connects to devices at all — see
`app/sync/liveness.py` for what replaced it.

So this is a laptop-in-the-office diagnostic, not something the service runs
and not something to wire into a health check. `pyzk` is deliberately no longer
in requirements.txt; install it by hand when you need this.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

try:
    from zk import ZK
except ImportError:
    print("\n" + "=" * 70)
    print("[ERROR] The 'pyzk' library is not installed in this virtual environment.")
    print("Please run the following command to install it first:")
    print("  .\\venv\\Scripts\\pip.exe install pyzk")
    print("=" * 70 + "\n")
    sys.exit(1)


DEVICE_IP = os.getenv("DEVICE_IP", "192.168.1.209")
PORT = int(os.getenv("DEVICE_PORT", "4370"))


def list_admins():
    print(f"Connecting to biometric device at {DEVICE_IP}:{PORT}...")
    # ommit_ping=True: skip pyzk's ICMP probe and just try the connection.
    # Plenty of these readers drop ICMP while answering on 4370 perfectly well,
    # so the ping was only ever a way to fail before the real attempt — and the
    # connection either works or it does not, which is the answer anyway.
    zk = ZK(
        DEVICE_IP,
        port=PORT,
        timeout=10,
        force_udp=False,
        ommit_ping=True
    )

    conn = None
    try:
        conn = zk.connect()
        print("Connected successfully!")

        conn.disable_device()
        users = conn.get_users()
        conn.enable_device()

        print("\n" + "=" * 70)
        print(f"  ADMINISTRATOR-RELATED USERS ON TERMINAL ({len(users)} TOTAL USERS)")
        print("=" * 70)
        print(f"{'User ID':<10} | {'Name':<22} | {'Privilege':<10} | {'Role Type'}")
        print("-" * 70)

        admin_count = 0
        for user in users:
            is_admin = False
            role_type = "Normal User"

            # Privilege definitions in pyzk:
            # 0: Normal User
            # 14: Super Admin
            # 2: Manager/Registrar
            # 6: Documenter
            if user.privilege == 14:
                role_type = "Super Admin"
                is_admin = True
            elif user.privilege == 2:
                role_type = "Registrar/Manager"
                is_admin = True
            elif user.privilege == 6:
                role_type = "Documenter"
                is_admin = True
            elif user.privilege > 0:
                role_type = f"Admin (Type {user.privilege})"
                is_admin = True
            elif user.user_id == "111":
                role_type = "Normal User (Admin override target 111)"
                is_admin = True

            if is_admin:
                admin_count += 1
                name_disp = user.name if user.name else "N/A"
                print(f"{user.user_id:<10} | {name_disp:<22} | {user.privilege:<10} | {role_type}")

        print("-" * 70)
        print(f"Total admin-related users found on device: {admin_count}")
        print("=" * 70 + "\n")

    except Exception as e:
        print(f"\n[ERROR] Error connecting or communicating with device: {e}\n")
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    list_admins()
