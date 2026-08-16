import os

import psycopg2
from dotenv import load_dotenv

# Load database settings from .env file
load_dotenv()

DB_NAME = os.getenv("DB_NAME", "gym")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")

# Super Admin PINs to unblock
SUPERADMIN_PINS = [0, 1, 2, 6, 7, 8, 17, 79, 111, 784, 855, 5050, 9999]
DEVICE_SN = os.getenv("DEVICE_SN", "NFZ8254900401")


def unblock() -> None:
    print(f"Connecting to database '{DB_NAME}' at {DB_HOST}:{DB_PORT}...")
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
        )
        cur = conn.cursor()

        print(f"Queueing unblock (Grp=1) commands for Super Admins on device {DEVICE_SN}...")

        count = 0
        for pin in SUPERADMIN_PINS:
            cmd_text = f"DATA UPDATE USERINFO PIN={pin}\tGrp=1"

            # Check if this command is already pending to avoid duplicates
            cur.execute(
                """
                SELECT COUNT(*) FROM public.biometric_commands
                WHERE device_sn = %s AND command_text = %s AND processed = FALSE;
            """,
                (DEVICE_SN, cmd_text),
            )

            if cur.fetchone()[0] == 0:
                cur.execute("""
                    INSERT INTO public.biometric_commands (device_sn, command_text, processed)
                    VALUES (%s, %s, FALSE);
                """, (DEVICE_SN, cmd_text))
                print(f"  [QUEUED] Unblock command for Super Admin ID: {pin}")
                count += 1
            else:
                print(f"  [SKIPPED] Unblock command already pending for Super Admin ID: {pin}")

        conn.commit()
        cur.close()
        conn.close()
        print(f"\nSuccessfully queued {count} new unblock commands. Device will pull them on its next poll.")

    except Exception as e:
        print(f"\n[ERROR] Failed to queue unblock commands: {e}\n")


if __name__ == "__main__":
    unblock()
