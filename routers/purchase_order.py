# purchase_order_router.py

from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import List, Dict, Optional, Literal # Import Literal
from decimal import Decimal
import json
import sys
import os
import httpx
import logging
from datetime import datetime

# --- Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Ensure the database module can be found
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db_connection

# --- Auth and Service URL Configuration ---
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="https://bleu-ums.onrender.com/auth/token")
USER_SERVICE_ME_URL = "https://bleu-ums.onrender.com/auth/users/me"

# --- Define the new router ---
router_purchase_order = APIRouter(
    prefix="/auth/purchase_orders",
    tags=["Purchase Orders"]
)

# --- Authorization Helper Function ---
# ... (this function is unchanged)
async def get_current_active_user(token: str = Depends(oauth2_scheme)):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(USER_SERVICE_ME_URL, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Invalid token or user not found: {e.response.text}", headers={"WWW-Authenticate": "Bearer"})
        except httpx.RequestError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to the authentication service.")
    return response.json()

# --- Pydantic Models ---
class ProcessingSaleItem(BaseModel):
    name: str
    quantity: int
    price: float
    category: str
    addons: Optional[dict] = {}

class ProcessingOrder(BaseModel):
    id: str
    date: str
    items: int
    total: float
    status: str
    orderType: str
    paymentMethod: str
    cashierName: str
    GCashReferenceNumber: Optional[str] = None

    orderItems: List[ProcessingSaleItem]

# --- NEW: Pydantic model for the status update request body ---
class UpdateOrderStatusRequest(BaseModel):
    # Use Literal to restrict the possible values for the new status
    newStatus: Literal["completed", "cancelled"]


# --- API Endpoint to Get Processing Orders ---
# ... (this function is unchanged)
@router_purchase_order.get(
    "/status/processing",
    response_model=List[ProcessingOrder],
    summary="Get Processing Orders with Optional Cashier Filter"
)
async def get_processing_orders(
    cashierName: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user)
):
    """
    Retrieves sales with 'processing' status, with role-based filtering.
    - **Admins/Managers**: Can see all orders. Can optionally filter by `cashierName`.
    - **Cashiers/Staff**: Can only see their own orders. The `cashierName` parameter is ignored.
    """
    allowed_roles = ["admin", "manager", "staff", "cashier"]
    user_role = current_user.get("userRole")
    if user_role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view orders."
        )

    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            logged_in_username = current_user.get("username")

            sql = """
                SELECT
                    s.SaleID, s.OrderType, s.PaymentMethod, s.CreatedAt, s.CashierName,
                    s.TotalDiscountAmount, s.Status,
                    si.SaleItemID, si.ItemName, si.Quantity, si.UnitPrice, si.Category, si.Addons
                FROM Sales AS s
                LEFT JOIN SaleItems AS si ON s.SaleID = si.SaleID
                WHERE s.Status IN ('completed', 'processing')
            """
            params = []

            if user_role in ["admin", "manager"]:
                if cashierName:
                    sql += " AND s.CashierName = ? "
                    params.append(cashierName)
            else:
                sql += " AND s.CashierName = ? "
                params.append(logged_in_username)

            sql += " ORDER BY s.CreatedAt ASC, s.SaleID ASC;"
            
            await cursor.execute(sql, *params)
            rows = await cursor.fetchall()

            orders_dict: Dict[int, dict] = {}
            item_subtotals: Dict[int, Decimal] = {}

            for row in rows:
                sale_id = row.SaleID
                if sale_id not in orders_dict:
                    item_subtotals[sale_id] = Decimal('0.0')
                    orders_dict[sale_id] = {
                        "id": f"SO-{sale_id}",
                        "date": row.CreatedAt.strftime("%B %d, %Y %I:%M %p"),
                        "status": row.Status,
                        "orderType": row.OrderType,
                        "paymentMethod": row.PaymentMethod,
                        "cashierName": row.CashierName,
                        "items": 0,
                        "orderItems": [],
                        "_totalDiscount": row.TotalDiscountAmount,
                    }

                if row.SaleItemID:
                    item_quantity = row.Quantity or 0
                    item_price = row.UnitPrice or Decimal('0.0')
                    orders_dict[sale_id]["items"] += item_quantity
                    item_subtotals[sale_id] += item_price * item_quantity
                    orders_dict[sale_id]["orderItems"].append(
                        ProcessingSaleItem(
                            name=row.ItemName,
                            quantity=item_quantity,
                            price=float(item_price),
                            category=row.Category,
                            addons=json.loads(row.Addons) if row.Addons else {}
                        )
                    )

            response_list = []
            for sale_id, order_data in orders_dict.items():
                subtotal = item_subtotals.get(sale_id, Decimal('0.0'))
                total_discount = order_data.pop("_totalDiscount", Decimal('0.0'))
                final_total = subtotal - total_discount
                order_data["total"] = float(final_total)
                response_list.append(ProcessingOrder(**order_data))

            return response_list

    except Exception as e:
        logger.error(f"Error fetching processing orders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch processing orders.")
    finally:
        if conn:
            await conn.close()


# In purchase_order_router.py

# --- NEW: Function to change the status of an order ---
@router_purchase_order.patch(
    "/{order_id}/status",
    status_code=status.HTTP_200_OK,
    summary="Update the status of a specific order"
)
async def update_order_status(
    order_id: str,
    request: UpdateOrderStatusRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """
    Updates the status of an order to 'completed' or 'cancelled'.
    Accessible by admin, manager, staff, and cashier roles.
    """
    allowed_roles = ["admin", "manager", "staff", "cashier"]
    if current_user.get("userRole") not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update order status."
        )

    try:
        parsed_id = int(order_id.split('-')[-1])
    except (ValueError, IndexError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid order ID format: '{order_id}'. Expected format 'SO-XXX'."
        )

    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            # --- FIX: Removed the "UpdatedAt" field from the SQL query ---
            # The database 'Sales' table does not have this column.
            sql_update = """
                UPDATE Sales
                SET Status = ?
                WHERE SaleID = ?
            """
            await cursor.execute(sql_update, request.newStatus, parsed_id)
            
            if cursor.rowcount == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Order with ID '{order_id}' not found."
                )

            await conn.commit()
            
            return {"message": f"Order {order_id} status successfully updated to '{request.newStatus}'."}
            
    except Exception as e:
        if conn: await conn.rollback()
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error updating status for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An unexpected error occurred while updating the order status.")
    finally:
        if conn:
            await conn.close()


@router_purchase_order.get(
    "/all",
    response_model=List[ProcessingOrder],
    summary="Get All Orders (Admin/Manager Only)"
)
async def get_all_orders(current_user: dict = Depends(get_current_active_user)):
    """
    Retrieves all sales with 'processing' or 'completed' status.
    - **Admins/Managers**: Can see all orders from all cashiers.
    - **Other roles**: Access is forbidden.
    """
    allowed_roles = ["admin", "manager"]
    user_role = current_user.get("userRole")
    if user_role not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to view all orders.")

    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            # --- FIXED: Select the specific GCashReferenceNumber column directly ---
            sql = """
                SELECT
                    s.SaleID, s.OrderType, s.PaymentMethod, s.CreatedAt, s.CashierName,
                    s.TotalDiscountAmount, s.Status, s.GCashReferenceNumber,
                    si.SaleItemID, si.ItemName, si.Quantity, si.UnitPrice, si.Category, si.Addons
                FROM Sales AS s
                LEFT JOIN SaleItems AS si ON s.SaleID = si.SaleID
                WHERE s.Status IN ('completed', 'processing')
                ORDER BY s.CreatedAt DESC, s.SaleID DESC;
            """
            await cursor.execute(sql)
            rows = await cursor.fetchall()

            orders_dict: Dict[int, dict] = {}
            item_subtotals: Dict[int, Decimal] = {}

            for row in rows:
                sale_id = row.SaleID
                if sale_id not in orders_dict:
                    item_subtotals[sale_id] = Decimal('0.0')
                    # --- FIXED: Use the correct column name directly ---
                    orders_dict[sale_id] = {
                        "id": f"SO-{sale_id}",
                        "date": row.CreatedAt.strftime("%B %d, %Y %I:%M %p"),
                        "status": row.Status,
                        "orderType": row.OrderType,
                        "paymentMethod": row.PaymentMethod,
                        "cashierName": row.CashierName,
                        "GCashReferenceNumber": row.GCashReferenceNumber,
                        "items": 0,
                        "orderItems": [],
                        "_totalDiscount": row.TotalDiscountAmount,
                    }

                if row.SaleItemID:
                    item_quantity = row.Quantity or 0
                    item_price = row.UnitPrice or Decimal('0.0')
                    orders_dict[sale_id]["items"] += item_quantity
                    item_subtotals[sale_id] += item_price * item_quantity
                    orders_dict[sale_id]["orderItems"].append(
                        ProcessingSaleItem(
                            name=row.ItemName,
                            quantity=item_quantity,
                            price=float(item_price),
                            category=row.Category,
                            addons=json.loads(row.Addons) if row.Addons else {}
                        )
                    )

            response_list = []
            for sale_id, order_data in orders_dict.items():
                subtotal = item_subtotals.get(sale_id, Decimal('0.0'))
                total_discount = order_data.pop("_totalDiscount", Decimal('0.0'))
                final_total = subtotal - total_discount
                order_data["total"] = float(final_total)
                response_list.append(ProcessingOrder(**order_data))

            return response_list

    except Exception as e:
        logger.error(f"Error fetching all orders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch all orders.")
    finally:
        if conn:
            await conn.close()