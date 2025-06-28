# SalesServices/routers/pos_router.py

from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import List, Optional
from decimal import Decimal
import json
import sys
import os
import httpx
import logging 

# --- Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db_connection

# --- Auth and Service URL Configuration ---
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="https://bleu-ums.onrender.com/auth/token")
USER_SERVICE_ME_URL = "https://bleu-ums.onrender.com/auth/users/me"

# --- URLs for Inventory Deduction Endpoints ---
INGREDIENTS_DEDUCT_URL = "https://ingredient-services.onrender.com/ingredients/ingredients/deduct-from-sale"
MATERIALS_DEDUCT_URL = "https://material-service.onrender.com/materials/materials/deduct-from-sale"

router_sales = APIRouter(prefix="/auth/sales", tags=["sales"])

ADDON_PRICES = {
    'espressoShots': Decimal('25.00'),
    'seaSaltCream': Decimal('30.00'),
    'syrupSauces': Decimal('20.00'),
}

class SaleItem(BaseModel):
    name: str
    quantity: int
    price: float
    category: str
    addons: dict

class Sale(BaseModel):
    cartItems: List[SaleItem]
    orderType: str
    paymentMethod: str
    appliedDiscounts: List[str]
    gcashReference: Optional[str] = None

# --- Authorization Helper Function ---
async def get_current_active_user(token: str = Depends(oauth2_scheme)):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(USER_SERVICE_ME_URL, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"Invalid token or user not found: {e.response.text}",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except httpx.RequestError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not connect to the authentication service."
            )
    return response.json()


# --- Helper functions to call Inventory Services ---
async def trigger_ingredients_deduction(cart_items: List[SaleItem], token: str):
    logger.info("Triggering INGREDIENT deduction.")
    payload = {"cartItems": [{"name": item.name, "quantity": item.quantity} for item in cart_items]}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(INGREDIENTS_DEDUCT_URL, json=payload, headers=headers)
            response.raise_for_status()
            logger.info("Successfully requested INGREDIENT deduction.")
    except Exception as e:
        logger.critical(f"INGREDIENT-SYNC-FAILURE: Sale processed, but failed to deduct ingredients. Error: {e}")

async def trigger_materials_deduction(cart_items: List[SaleItem], token: str):
    logger.info("Triggering MATERIAL deduction.")
    payload = {"cartItems": [{"name": item.name, "quantity": item.quantity} for item in cart_items]}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(MATERIALS_DEDUCT_URL, json=payload, headers=headers)
            response.raise_for_status()
            logger.info("Successfully requested MATERIAL deduction.")
    except Exception as e:
        logger.critical(f"MATERIAL-SYNC-FAILURE: Sale processed, but failed to deduct materials. Error: {e}")


# --- Helper function for calculations ---
async def calculate_totals_and_discounts(sale_data: Sale, cursor):
    subtotal = Decimal('0.0')
    for item in sale_data.cartItems:
        item_price = Decimal(str(item.price))
        addons_price = Decimal('0.0')
        if item.addons:
            for addon_name, quantity in item.addons.items():
                addons_price += ADDON_PRICES.get(addon_name, Decimal('0.0')) * quantity
        subtotal += (item_price + addons_price) * item.quantity

    total_discount_amount = Decimal('0.0')
    applied_discounts_details = []

    if not sale_data.appliedDiscounts:
        return subtotal, total_discount_amount, applied_discounts_details

    placeholders = ','.join(['?' for _ in sale_data.appliedDiscounts])
    sql_fetch_discounts = f"""
        SELECT id, name, discount_type, discount_value, minimum_spend
        FROM discounts
        WHERE name IN ({placeholders}) AND status = 'active'
    """
    await cursor.execute(sql_fetch_discounts, sale_data.appliedDiscounts)
    valid_discounts = await cursor.fetchall()

    for discount in valid_discounts:
        min_spend = discount.minimum_spend or Decimal('0.0')
        if subtotal >= min_spend:
            discount_value = Decimal('0.0')
            if discount.discount_type == 'percentage' and discount.discount_value is not None:
                discount_value = (subtotal * Decimal(str(discount.discount_value))) / Decimal('100')
            elif discount.discount_type == 'fixed_amount' and discount.discount_value is not None:
                discount_value = Decimal(str(discount.discount_value))
            
            total_discount_amount += discount_value
            applied_discounts_details.append({"id": discount.id, "amount": discount_value})

    final_discount = min(total_discount_amount, subtotal)
    return subtotal, final_discount, applied_discounts_details

# --- API Endpoint to Create a Sale ---
@router_sales.post("/", status_code=status.HTTP_201_CREATED)
async def create_sale(
    sale: Sale, 
    token: str = Depends(oauth2_scheme),
    current_user: dict = Depends(get_current_active_user)
):
    allowed_roles = ["admin", "manager", "staff", "cashier"]
    if current_user.get("userRole") not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to create a sale."
        )

    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            subtotal, total_discount, discount_details = await calculate_totals_and_discounts(sale, cursor)
            cashier_name = current_user.get("username", "SystemUser")

            sql_sale = """
                INSERT INTO Sales (OrderType, PaymentMethod, CashierName, TotalDiscountAmount, GCashReferenceNumber) 
                OUTPUT INSERTED.SaleID 
                VALUES (?, ?, ?, ?, ?)
            """
            await cursor.execute(
                sql_sale, 
                sale.orderType, 
                sale.paymentMethod, 
                cashier_name, 
                total_discount, 
                sale.gcashReference
            )
            sale_id_row = await cursor.fetchone()
            if not sale_id_row or not sale_id_row[0]:
                raise HTTPException(status_code=500, detail="Failed to create sale record.")
            sale_id = sale_id_row[0]

            for item in sale.cartItems:
                # =================================================================================
                # THE FIX IS HERE: The table name is corrected to 'SaleItem' (singular).
                # =================================================================================
                sql_item = "INSERT INTO SaleItem (SaleID, ItemName, Quantity, UnitPrice, Category, Addons) VALUES (?, ?, ?, ?, ?, ?)"
                addons_str = json.dumps(item.addons) if item.addons else None
                await cursor.execute(sql_item, sale_id, item.name, item.quantity, Decimal(str(item.price)), item.category, addons_str)

            for discount in discount_details:
                sql_sale_discount = "INSERT INTO SaleDiscounts (SaleID, DiscountID, DiscountAppliedAmount) VALUES (?, ?, ?)"
                await cursor.execute(sql_sale_discount, sale_id, discount['id'], discount['amount'])

            await conn.commit()
            
            await trigger_ingredients_deduction(cart_items=sale.cartItems, token=token)
            await trigger_materials_deduction(cart_items=sale.cartItems, token=token)
            
            final_total = subtotal - total_discount
            return {
                "saleId": sale_id,
                "subtotal": float(subtotal),
                "discountAmount": float(total_discount),
                "finalTotal": float(final_total)
            }
    except Exception as e:
        if conn: await conn.rollback()
        logger.error(f"Error processing sale: {e}", exc_info=True)
        if not isinstance(e, HTTPException):
             raise HTTPException(status_code=500, detail=f"An unexpected error occurred while processing the sale.")
        raise e
    finally:
        if conn: await conn.close()
