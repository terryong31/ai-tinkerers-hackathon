from fastapi import FastAPI
from apis.nearby import router as nearby_router

app = FastAPI(title="Hospital Backend")

# Mount only the Nearby Hospitals API for now
app.include_router(nearby_router)

# Later you’ll add:
# from apis.wait_time import router as wait_router
# from apis.chatbot import router as chat_router
# app.include_router(wait_router)
# app.include_router(chat_router)
