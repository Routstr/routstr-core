from setuptools import find_packages, setup

setup(
    name="routstr",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "fastapi[standard]>=0.115",
        "aiosqlite>=0.20",
        "sqlmodel>=0.0.24",
        "httpx[socks]>=0.25.2",
        "greenlet>=3.2.1",
        "python-json-logger>=2.0.0",
        "cashu",
        "secp256k1",
        "marshmallow>=3.13,<4.0",
    ],
    python_requires=">=3.11",
)
