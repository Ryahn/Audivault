"""Start Audivault:  python run.py"""

import uvicorn

from app.core.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, proxy_headers=True, forwarded_allow_ips="*")
