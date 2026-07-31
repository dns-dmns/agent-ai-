import time
import json
import asyncio
import google.generativeai as genai

async def wait_for_ui_idle(page, max_wait_ms: int = 3000):
    """PILLAR 4: Smart DOM Mutation & Network Idle Observer. Pauses until loading overlays detach."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=max_wait_ms)
    except Exception:
        pass
        
    try:
        spinners = page.locator(".p-component-overlay, .spinner, .loading-bar, .p-progressbar, .p-skeleton")
        if await spinners.count() > 0:
            await spinners.first.wait_for(state="detached", timeout=max_wait_ms)
    except Exception:
        pass

def resolve_dynamic_variables(text_str: str, runtime_pool: dict) -> str:
    """PILLAR 1: Dynamic Variable Pool. Replaces {{token}} placeholders with runtime pool values."""
    if not isinstance(text_str, str) or not text_str:
        return text_str
    result = text_str
    for key, val in runtime_pool.items():
        placeholder = f"{{{{{key}}}}}"
        if placeholder in result and val:
            result = result.replace(placeholder, str(val))
    return result

async def vision_ai_locate_element(page, selector: str) -> tuple[int, int] | None:
    """PILLAR 2: Vision AI Semantic Selector Healing. Uses Gemini Vision to locate elements visually on failure."""
    try:
        screenshot_bytes = await page.screenshot(type="png")
        prompt = f"""
        Analyze this webpage screenshot.
        Locate the UI element matching this selector/description: '{selector}'
        Return ONLY a JSON object with normalized coordinate percentages (0 to 100) representing the center of the target element:
        {{"x_pct": 45.5, "y_pct": 62.0}}
        If the element is not visible or cannot be identified, return: {{"x_pct": 0, "y_pct": 0}}
        """
        
        model = genai.GenerativeModel(model_name='gemini-2.0-flash')
        response = await asyncio.to_thread(
            model.generate_content,
            [prompt, {"mime_type": "image/png", "data": screenshot_bytes}],
            generation_config={"response_mime_type": "application/json"}
        )
        
        data = json.loads(response.text.strip())
        x_pct = float(data.get("x_pct", 0))
        y_pct = float(data.get("y_pct", 0))
        
        if x_pct > 0 and y_pct > 0:
            viewport = page.viewport_size or {"width": 1280, "height": 720}
            x_px = int((x_pct / 100.0) * viewport["width"])
            y_px = int((y_pct / 100.0) * viewport["height"])
            return (x_px, y_px)
    except Exception as e:
        print(f"Vision AI locator fallback notice: {e}")
        
    return None

async def attempt_auto_healing(page, action: str, selector: str, value: str = "") -> bool:
    """PILLAR 5 & 2: Self-Correcting Auto-Repair Loop + Vision AI Fallback."""
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        await page.evaluate("() => { const b = document.querySelector('.p-dialog-mask, .modal-backdrop'); if (b) b.click(); }")
    except Exception:
        pass

    if action == "TYPE":
        try:
            clean_id = selector.lstrip("#").strip()
            candidates = [selector, f"input[name='{clean_id}']", f"input[placeholder*='{clean_id}']", f"textarea[name='{clean_id}']"]
            for cand in candidates:
                loc = page.locator(cand).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.fill(value, timeout=3000)
                    return True
        except Exception:
            pass
            
        # Pillar 2: Vision AI Locator Fallback for Input
        coords = await vision_ai_locate_element(page, selector)
        if coords:
            try:
                await page.mouse.click(coords[0], coords[1])
                await page.keyboard.type(value)
                return True
            except Exception:
                pass
    elif action == "CLICK":
        try:
            candidates = [selector, f"button:has-text('{selector}')", f"a:has-text('{selector}')", ".p-dialog-header-close", ".btn-close"]
            for cand in candidates:
                loc = page.locator(cand).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=3000)
                    return True
        except Exception:
            pass
            
        # Pillar 2: Vision AI Locator Fallback for Click
        coords = await vision_ai_locate_element(page, selector)
        if coords:
            try:
                await page.mouse.click(coords[0], coords[1])
                return True
            except Exception:
                pass

    return False
