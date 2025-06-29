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

# --- NEW: Pydantic models for receiving an online order ---
class OnlineSaleItem(BaseModel):
    name: str
    quantity: int
    price: float
    category: Optional[str] = "Online" # Default category
    addons: Optional[dict] = {}

class OnlineOrderRequest(BaseModel):
    online_order_id: int
    customer_name: str
    order_type: str
    payment_method: str
    subtotal: float
    total_amount: float
    status: str
    items: List[OnlineSaleItem]

# --- NEW: Pydantic model for the status update request body ---
class UpdateOrderStatusRequest(BaseModel):
    # Use Literal to restrict the possible values for the new status
    newStatus: Literal["completed", "cancelled", "processing"] # Added processing for flexibility


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
            WHERE s.Status IN ('processing', 'completed')
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


# --- NEW: Endpoint to receive and save an online order ---
@router_purchase_order.post(
    "/online-order",
    status_code=status.HTTP_201_CREATED,
    summary="Save an online order to the POS system"
)
async def save_online_order(
    order_data: OnlineOrderRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """
    Receives an order from the online/cart service and saves it into the
    local POS database (`Sales` and `SaleItems` tables). This operation
    is transactional.
    """
    allowed_roles = ["admin", "staff", "cashier"]
    if current_user.get("userRole") not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to create orders."
        )

    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            # Start a transaction
            await conn.begin()

            # 1. Insert into Sales table
            discount_amount = Decimal(order_data.subtotal) - Decimal(order_data.total_amount)
            
            # Use customer_name as the CashierName for identification
            # Use online_order_id in GCashReferenceNumber as a way to link back
            sql_insert_sale = """
                INSERT INTO Sales (
                    OrderType, PaymentMethod, CashierName, TotalDiscountAmount, Status,
                    GCashReferenceNumber, CreatedAt
                )
                OUTPUT INSERTED.SaleID
                VALUES (?, ?, ?, ?, ?, ?, GETDATE())
            """
            await cursor.execute(
                sql_insert_sale,
                order_data.order_type,
                order_data.payment_method,
                order_data.customer_name,
                discount_amount,
                order_data.status,
                f"ONLINE-{order_data.online_order_id}"
            )
            
            sale_id_row = await cursor.fetchone()
            if not sale_id_row:
                raise Exception("Failed to create sale record and retrieve new SaleID.")
            new_sale_id = sale_id_row.SaleID

            # 2. Insert into SaleItems table for each item in the order
            sql_insert_item = """
                INSERT INTO SaleItems (SaleID, ItemName, Quantity, UnitPrice, Category, Addons)
                VALUES (?, ?, ?, ?, ?, ?)
            """
            for item in order_data.items:
                await cursor.execute(
                    sql_insert_item,
                    new_sale_id,
                    item.name,
                    item.quantity,
                    Decimal(item.price),
                    item.category,
                    json.dumps(item.addons) if item.addons else None
                )
            
            # Commit the transaction if all inserts were successful
            await conn.commit()
            
            logger.info(f"Successfully saved online order {order_data.online_order_id} as POS SaleID {new_sale_id}")
            return {
                "message": "Online order successfully saved to POS",
                "pos_sale_id": new_sale_id
            }

    except Exception as e:
        if conn:
            await conn.rollback() # Roll back the transaction on any error
        logger.error(f"Failed to save online order to POS: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while saving the online order: {e}"
        )
    finally:
        if conn:
            await conn.close()


# In purchase_order_router.py

# --- Function to change the status of an order ---
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
        # This logic handles both "SO-123" and just "123"
        parsed_id = int(order_id.split('-')[-1])
    except (ValueError, IndexError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid order ID format: '{order_id}'. Expected format 'SO-XXX' or numeric ID."
        )

    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            sql_update = """
                UPDATE Sales
                SET Status = ?, UpdatedAt = GETDATE()
                WHERE SaleID = ?
            """
            # Note: Ensure your 'Sales' table has an 'UpdatedAt' column (datetime2)
            # If not, remove it from the query: SET Status = ?
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
                WHERE s.Status IN ('completed', 'processing', 'cancelled')
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
