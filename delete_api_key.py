import os
import asyncio
from typing import Optional

from sqlmodel import Field, SQLModel, create_engine, Session

# Define the ApiKey model, replicating the structure from proxy/router/db.py
class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    hashed_key: str = Field(primary_key=True)
    balance: int = Field(default=0)
    refund_address: Optional[str] = Field(default=None)
    key_expiry_time: Optional[int] = Field(default=None)
    total_spent: int = Field(default=0)
    total_requests: int = Field(default=0)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///keys.db")
engine = create_engine(DATABASE_URL, echo=False)

def delete_api_key(hashed_key_to_delete: str):
    with Session(engine) as session:
        api_key = session.get(ApiKey, hashed_key_to_delete)
        if api_key:
            session.delete(api_key)
            session.commit()
            print(f"API key with hashed_key '{hashed_key_to_delete}' deleted successfully.")
        else:
            print(f"API key with hashed_key '{hashed_key_to_delete}' not found.")

if __name__ == "__main__":
    # Replace 'YOUR_HASHED_KEY_HERE' with the actual hashed key you want to delete
    hashed_key_to_delete = "bbd53eb76dad24aae6e5c13c938486cd674618e6a14ecbd2776e820c3c19fbfb"
    delete_api_key(hashed_key_to_delete)