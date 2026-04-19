import os
import sys
import logging
import requests
import json
import re
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth_sync
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env si existe
load_dotenv()

# Configuración de logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Array de productos por defecto (Vacío por seguridad)
PRODUCTS_TO_MONITOR = []

# Intentar cargar los productos secretos desde las variables de entorno
products_env = os.getenv("PRODUCTS_JSON")
if products_env:
    try:
        PRODUCTS_TO_MONITOR = json.loads(products_env)
        logger.info("Productos cargados exitosamente desde variable secreta.")
    except Exception as e:
        logger.error("Error leyendo PRODUCTS_JSON. Se usará la lista por defecto.")

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
AUTH_FILE = "auth.json"

def send_discord_notification(product_name, product_url, image_url, price, size, screenshot_path):
    if not DISCORD_WEBHOOK:
        logger.warning("DISCORD_WEBHOOK no proporcionado. Omitiendo notificación.")
        return

    logger.info("Enviando webhook a Discord con Embed profesional...")
    
    uniqlo_red = 16711680

    embed = {
        "title": f"🚀 RESTOCK DETECTADO: {product_name}",
        "url": product_url,
        "color": uniqlo_red,
        "fields": [
            {"name": "🏬 Tienda", "value": "Uniqlo", "inline": True},
            {"name": "📏 Talla", "value": size, "inline": True},
            {"name": "💰 Precio", "value": price if price else "N/A", "inline": True},
            {"name": "🟢 Estado", "value": "✅ Disponible", "inline": True},
            {"name": "🛒 Enlaces Rápidos", "value": f"[Ir al Producto]({product_url}) | [Ir al Carrito](https://www.uniqlo.com/es/es/cart)", "inline": False}
        ],
        "footer": {
            "text": "Monitoreo de Alta Prioridad"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    if image_url:
        embed["image"] = {"url": image_url}

    payload = {
        "content": "@everyone ¡Nuevo restock encontrado!",
        "embeds": [embed]
    }
    
    try:
        response = requests.post(DISCORD_WEBHOOK, json=payload)
        response.raise_for_status()
        logger.info("Notificación enviada exitosamente a Discord sin captura.")
    except Exception as e:
        logger.error(f"Fallo al enviar webhook: {e}")

def check_restock():
    if not PRODUCTS_TO_MONITOR:
        logger.error("No hay productos configurados en PRODUCTS_TO_MONITOR.")
        return

    logger.info(f"Monitor iniciado -> Revisando {len(PRODUCTS_TO_MONITOR)} producto(s).")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        context_options = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080}
        }
        
        if os.path.exists(AUTH_FILE):
            logger.info(f"Cargando sesión desde {AUTH_FILE}")
            context_options["storage_state"] = AUTH_FILE
        else:
            logger.warning(f"No se encontró {AUTH_FILE}. Navegando como invitado.")

        context = browser.new_context(**context_options)
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for idx, product in enumerate(PRODUCTS_TO_MONITOR):
            url = product.get("url")
            target_size = product.get("size")
            
            logger.info(f"[{idx+1}/{len(PRODUCTS_TO_MONITOR)}] Revisando {url} | Talla: {target_size}")
            
            page = context.new_page()
            Stealth().apply_stealth_sync(page)
            
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                
                if response and response.status == 403:
                    logger.error("Error 403: Bloqueo de IP (Posible Cloudflare/Akamai).")
                    continue

                if "Access Denied" in page.title():
                    logger.error("Acceso denegado. El WAF detectó el bot.")
                    continue
                    
                try:
                    product_name_el = page.wait_for_selector("h1", timeout=5000)
                    product_name = product_name_el.inner_text().strip() if product_name_el else "Producto Uniqlo"
                except PlaywrightTimeoutError:
                    product_name = "Producto Uniqlo (H1 No encontrado)"
                    
                logger.info(f"Página cargada. Producto: {product_name}")

                try:
                    size_element = page.locator("button, label").filter(has_text=re.compile(f"^{target_size}$")).first
                    size_element.wait_for(state="visible", timeout=10000)
                    
                    is_disabled = False
                    if size_element.get_attribute("disabled") is not None:
                        is_disabled = True
                        
                    class_attr = (size_element.get_attribute("class") or "").lower()
                    if any(cls in class_attr for cls in ["disabled", "out-of-stock", "unavailable"]):
                        is_disabled = True
                        
                    if not is_disabled:
                        input_id = size_element.get_attribute("for")
                        if input_id:
                            linked_input = page.locator(f"//input[@id='{input_id}']").first
                            if linked_input.count() > 0 and linked_input.get_attribute("disabled") is not None:
                                is_disabled = True
                    
                    if is_disabled:
                        logger.info(f"Check finalizado: Sin stock para la talla {target_size}.")
                        continue
                    
                    logger.info(f"¡STOCK ENCONTRADO! Añadiendo talla {target_size} al carrito...")
                    size_element.scroll_into_view_if_needed()
                    size_element.click()
                    page.wait_for_timeout(1000)
                    
                    add_button_xpath = "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'añadir a la cesta') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to bag')]"
                    add_to_cart_btn = page.locator(add_button_xpath).first
                    
                    try:
                        add_to_cart_btn.wait_for(state="visible", timeout=5000)
                        if add_to_cart_btn.is_enabled():
                            add_to_cart_btn.click()
                            logger.info("Click en 'Añadir a la cesta' ejecutado.")
                            page.wait_for_timeout(4000)
                            
                            try:
                                image_url = page.locator('meta[property="og:image"]').get_attribute("content", timeout=2000) or ""
                            except:
                                image_url = ""
                                
                            try:
                                price_el = page.locator('.price, [class*="price-text"], .fr-ec-price, .fr-ec-price__original-price').first
                                if price_el.is_visible(timeout=2000):
                                    price = price_el.inner_text().strip()
                                else:
                                    price = page.locator('meta[property="product:price:amount"]').get_attribute("content", timeout=2000)
                                    price = f"{price} €" if price else "Desconocido"
                            except:
                                price = "Desconocido"
                                
                            send_discord_notification(product_name, url, image_url, price, target_size, None)
                        else:
                            logger.error("El botón 'Añadir a la cesta' está visible pero deshabilitado.")
                    except PlaywrightTimeoutError:
                        logger.error("El botón 'Añadir a la cesta' no apareció a tiempo.")
                        
                except PlaywrightTimeoutError:
                    logger.info(f"Check finalizado: No existe la talla {target_size} o el selector falló.")
                    
            except Exception as e:
                logger.error(f"Error procesando {url}: {e}")
            
            finally:
                page.close()
                
        browser.close()

if __name__ == "__main__":
    check_restock()
