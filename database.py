import aioodbc

# database config
server = 'retailauth.c3wegea8y1ra.ap-southeast-2.rds.amazonaws.com'
database = 'bleuPOS'
username = 'bleuadmin'
password = 'Bleuauth123'
driver = 'ODBC Driver 17 for SQL Server'

# async function to get db connection
async def get_db_connection():
    dsn = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
    )
    conn = await aioodbc.connect(dsn=dsn, autocommit=True)
    return conn