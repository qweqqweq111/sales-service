from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# --- FIX: Correct the imports to match your filenames EXACTLY ---
# We are importing the modules 'sales_router' and 'purchase_order' from the 'routers' package.
from routers import pos_router, purchase_order

app = FastAPI(
    title="POS and Order Service API",
    description="Handles sales creation and retrieves processing orders.",
    version="1.0.0"
)

# --- Include routers using the correct imported objects ---
# The prefixes are defined inside the router files, so we don't add them here.

# This router is from sales_router.py and its object is named 'router_sales'
app.include_router(pos_router.router_sales)

# This router is from purchase_order.py and its object is named 'router_purchase_order'
app.include_router(purchase_order.router_purchase_order)


# Your CORS middleware is good. No changes needed here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bleu-pos-eight.vercel.app", # pos frontend
        "https://bleu-ums.onrender.com",
        "https://ingredient-services.onrender.com",
        "https://material-service.onrender.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# A simple root endpoint to easily check if the server is running
@app.get("/", tags=["Health Check"])
def read_root():
    return {"status": "ok", "message": "POS Service is running."}


# Run app
if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run("main:app", port=9000, host="0.0.0.0", reload=True)