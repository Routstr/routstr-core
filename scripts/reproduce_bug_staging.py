import asyncio

import httpx

BASE_URL = input("Enter routstr URL: ")
API_KEY = input("Enter key or token: ")


async def get_balance(client: httpx.AsyncClient) -> int:
    response = await client.get("/v1/balance/info")
    response.raise_for_status()
    data = response.json()
    print(f"Current Balance Info: {data}")
    return data.get("reserved", 0)


async def reproduce() -> None:
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(
        base_url=BASE_URL, headers=headers, timeout=30.0
    ) as client:
        print("Checking initial balance...")
        try:
            initial_reserved = await get_balance(client)
        except Exception as e:
            print(f"Failed to get balance: {e}")
            return

        print("\nStarting streaming request...")
        try:
            # Create a separate client for the stream so we can close it independently if needed,
            # but usually just breaking the loop and exiting the context manager is enough.
            # However, to be sure we simulate a harsh disconnect, we can just cancel the task or close the client.

            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-5-nano",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Write a long poem about the ocean.",
                        }
                    ],
                    "stream": True,
                },
            ) as response:
                print(f"Stream status: {response.status_code}")
                if response.status_code != 200:
                    print(f"Error: {await response.aread()}")
                    return

                print("Stream started. Reading a few chunks...")
                count = 0
                async for chunk in response.aiter_bytes():
                    print(f"Received chunk: {len(chunk)} bytes")
                    count += 1
                    if count >= 3:
                        print("Simulating client disconnect (breaking stream)...")
                        break

        except Exception as e:
            print(f"Stream interrupted (expected): {e}")

        # Wait a bit for the server to realize we disconnected (though with asyncio it might be immediate or depend on keepalive)
        print("\nWaiting for server to process disconnect...")
        await asyncio.sleep(5)

        print("\nChecking final balance...")
        try:
            final_reserved = await get_balance(client)
        except Exception:
            # Retry once if connection was closed
            async with httpx.AsyncClient(
                base_url=BASE_URL, headers=headers, timeout=30.0
            ) as new_client:
                final_reserved = await get_balance(new_client)

        if final_reserved > initial_reserved:
            print(
                f"\n[FAIL] Bug reproduced! Reserved balance increased: {initial_reserved} -> {final_reserved}"
            )
            print(f"Accumulated reserved balance: {final_reserved - initial_reserved}")
        else:
            print(
                f"\n[PASS] Reserved balance released correctly: {initial_reserved} -> {final_reserved}"
            )


if __name__ == "__main__":
    try:
        asyncio.run(reproduce())
    except KeyboardInterrupt:
        pass
