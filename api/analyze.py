"""
AgentReady Backend - Analizador real de webs para IA
Vercel Serverless Function (FastAPI)
"""

import json
import re
import time
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
nfrom pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup

app = FastAPI(title="AgentReady API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    url: str


class CheckResult(BaseModel):
    name: str
    icon: str
    pass_: bool
    fail_desc: str
    fix: str
    locked: bool = False


class AnalysisResponse(BaseModel):
    url: str
    score: int
    status: str
    checks: list
    failed_count: int
    passed_count: int
    analysis_time_ms: int


async def fetch_url(url: str, timeout: float = 10.0):
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={
            "User-Agent": "AgentReady-Bot/1.0 (Web Analyzer for AI Agents)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
        },
    ) as client:
        try:
            resp = await client.get(url)
            return resp.status_code, resp.text, dict(resp.headers)
        except Exception as e:
            return 0, str(e), {}


async def fetch_robots_txt(base_url: str):
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        try:
            resp = await client.get(robots_url)
            if resp.status_code == 200:
                return True, resp.text
            return False, ""
        except:
            return False, ""


async def fetch_sitemap(base_url: str):
    parsed = urlparse(base_url)
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        try:
            resp = await client.get(sitemap_url)
            if resp.status_code == 200:
                urls = re.findall(r'<loc>([^<]+)</loc>', resp.text)
                return True, len(urls)
            return False, 0
        except:
            return False, 0


def analyze_meta_tags(soup):
    result = {
        "has_title": False, "title_length": 0,
        "has_meta_description": False, "meta_description_length": 0,
        "has_viewport": False, "has_canonical": False, "has_charset": False,
    }
    title = soup.find("title")
    if title and title.string:
        result["has_title"] = True
        result["title_length"] = len(title.string.strip())
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        result["has_meta_description"] = True
        result["meta_description_length"] = len(meta_desc["content"])
    result["has_viewport"] = soup.find("meta", attrs={"name": "viewport"}) is not None
    result["has_canonical"] = soup.find("link", attrs={"rel": "canonical"}) is not None
    result["has_charset"] = soup.find("meta", charset=True) is not None
    return result


def analyze_open_graph(soup):
    og_tags = ["og:title", "og:description", "og:image", "og:url", "og:type"]
    result = {tag: False for tag in og_tags}
    for tag in og_tags:
        meta = soup.find("meta", attrs={"property": tag})
        if meta and meta.get("content"):
            result[tag] = True
    result["complete"] = all(result.values())
    return result


def analyze_schema_org(soup):
    result = {"has_jsonld": False, "types": [], "count": 0}
    jsonld_scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    result["has_jsonld"] = len(jsonld_scripts) > 0
    result["count"] = len(jsonld_scripts)
    for script in jsonld_scripts:
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, dict) and data.get("@type"):
                result["types"].append(data["@type"])
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type"):
                        result["types"].append(item["@type"])
        except:
            pass
    microdata = soup.find_all(attrs={"itemscope": True})
    if microdata:
        result["has_microdata"] = True
        result["count"] += len(microdata)
    return result


def analyze_headings(soup):
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    result = {"h1_count": 0, "h2_count": 0, "h3_count": 0, "total": len(headings),
              "has_h1": False, "multiple_h1": False, "structure_ok": True}
    for h in headings:
        level = int(h.name[1])
        if level == 1: result["h1_count"] += 1
        elif level == 2: result["h2_count"] += 1
        elif level == 3: result["h3_count"] += 1
    result["has_h1"] = result["h1_count"] > 0
    result["multiple_h1"] = result["h1_count"] > 1
    prev_level = 0
    for h in headings:
        level = int(h.name[1])
        if level > prev_level + 1:
            result["structure_ok"] = False
        prev_level = level
    return result


def analyze_images(soup):
    images = soup.find_all("img")
    result = {"total": len(images), "with_alt": 0, "without_alt": 0}
    for img in images:
        alt = img.get("alt", "")
        if alt and len(alt) > 3:
            result["with_alt"] += 1
        else:
            result["without_alt"] += 1
    return result


def analyze_links(soup, base_url):
    links = soup.find_all("a", href=True)
    parsed_base = urlparse(base_url)
    result = {"total": len(links), "internal": 0, "external": 0, "empty": 0}
    for link in links:
        href = link["href"]
        if href.startswith("#") or href == "/" or not href:
            result["empty"] += 1
            continue
        full_url = urljoin(base_url, href)
        parsed_link = urlparse(full_url)
        if parsed_link.netloc == parsed_base.netloc:
            result["internal"] += 1
        else:
            result["external"] += 1
    return result


def analyze_breadcrumbs(soup):
    breadcrumbs = soup.find_all(class_=re.compile(r"breadcrumb", re.I))
    jsonld_scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    has_breadcrumb_schema = False
    for script in jsonld_scripts:
        try:
            data = json.loads(script.string or "{}")
            items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "BreadcrumbList":
                    has_breadcrumb_schema = True
        except:
            pass
    return {"has_breadcrumbs": len(breadcrumbs) > 0, "has_breadcrumb_schema": has_breadcrumb_schema}


def analyze_contact_data(soup):
    jsonld_scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    has_org = False
    has_localbusiness = False
    for script in jsonld_scripts:
        try:
            data = json.loads(script.string or "{}")
            items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
            for item in items:
                if isinstance(item, dict):
                    schema_type = item.get("@type", "")
                    if schema_type in ["Organization", "Corporation"]:
                        has_org = True
                    elif schema_type == "LocalBusiness":
                        has_localbusiness = True
        except:
            pass
    return {"has_organization": has_org, "has_localbusiness": has_localbusiness, "has_contact_data": has_org or has_localbusiness}


def build_checks(url, status_code, html, robots_ok, sitemap_ok, sitemap_count):
    if status_code == 0:
        return [CheckResult(
            name="Error de conexión", icon="❌", pass_=False,
            fail_desc=f"No se pudo conectar con {url}.",
            fix="Verifica que la URL sea correcta.", locked=False
        )]

    soup = BeautifulSoup(html, "lxml")
    meta = analyze_meta_tags(soup)
    og = analyze_open_graph(soup)
    schema = analyze_schema_org(soup)
    headings = analyze_headings(soup)
    images = analyze_images(soup)
    links = analyze_links(soup, url)
    breadcrumbs = analyze_breadcrumbs(soup)
    contact = analyze_contact_data(soup)
    is_https = urlparse(url).scheme == "https"

    checks = []

    if not robots_ok:
        checks.append(CheckResult(
            name="Acceso bloqueado a crawlers de IA", icon="🤖", pass_=False,
            fail_desc="No se encontró robots.txt o bloquea el acceso. ChatGPT-User, ClaudeBot y PerplexityBot no pueden leer tu web.",
            fix="Crea un robots.txt en la raíz que permita: User-agent: * Allow: /", locked=False
        ))

    if not sitemap_ok:
        checks.append(CheckResult(
            name="Sin sitemap XML", icon="🗺️", pass_=False,
            fail_desc="No se detectó sitemap.xml. Los agentes de IA no pueden descubrir todas tus páginas.",
            fix="Genera un sitemap.xml con todas tus URLs públicas.", locked=False
        ))

    if not schema["has_jsonld"] and not schema.get("has_microdata", False):
        checks.append(CheckResult(
            name="Sin datos estructurados Schema.org", icon="📋", pass_=False,
            fail_desc="No se encontró marcado Schema.org. La IA no distingue entre precios, fechas, valoraciones o autores.",
            fix="Añade JSON-LD con Schema.org: Organization, Product, Article, FAQPage...", locked=True
        ))

    if not meta["has_meta_description"]:
        checks.append(CheckResult(
            name="Meta description ausente", icon="🏷️", pass_=False,
            fail_desc="Falta la meta description. La IA usa este campo para entender de qué trata la página.",
            fix="Añade: <meta name='description' content='Descripción de 150-160 caracteres'>", locked=True
        ))

    if meta["has_title"] and (meta["title_length"] < 10 or meta["title_length"] > 70):
        checks.append(CheckResult(
            name="Title mal optimizado", icon="📐", pass_=False,
            fail_desc=f"El title tiene {meta['title_length']} caracteres (ideal: 50-60).",
            fix="Usa un title único por página de 50-60 caracteres.", locked=True
        ))

    if not og["complete"]:
        missing = [k for k, v in og.items() if k.startswith("og:") and not v]
        checks.append(CheckResult(
            name="Open Graph incompleto", icon="📱", pass_=False,
            fail_desc=f"Faltan etiquetas Open Graph: {', '.join(missing)}.",
            fix="Añade og:title, og:description, og:image, og:url y og:type.", locked=True
        ))

    if not is_https:
        checks.append(CheckResult(
            name="Sin certificado HTTPS válido", icon="🔒", pass_=False,
            fail_desc="Tu web no usa HTTPS. Los navegadores y la IA la marcan como no segura.",
            fix="Instala un certificado SSL con Let's Encrypt.", locked=True
        ))

    if not meta["has_viewport"]:
        checks.append(CheckResult(
            name="No optimizado para móvil", icon="📱", pass_=False,
            fail_desc="Falta el meta viewport. Google y la IA usan mobile-first indexing.",
            fix="Añade: <meta name='viewport' content='width=device-width, initial-scale=1'>", locked=True
        ))

    if not headings["has_h1"]:
        checks.append(CheckResult(
            name="Sin heading H1", icon="📐", pass_=False,
            fail_desc="No se encontró H1. La IA usa headings para entender el contenido.",
            fix="Añade un único H1 por página.", locked=True
        ))
    elif headings["multiple_h1"]:
        checks.append(CheckResult(
            name="Múltiples H1", icon="📐", pass_=False,
            fail_desc=f"Hay {headings['h1_count']} H1 en la página. Debe haber solo uno.",
            fix="Consolida los H1 en uno solo. Usa H2-H6 para subsecciones.", locked=True
        ))

    if not breadcrumbs["has_breadcrumbs"] and not breadcrumbs["has_breadcrumb_schema"]:
        checks.append(CheckResult(
            name="Sin breadcrumb navigation", icon="🧭", pass_=False,
            fail_desc="No se detectó navegación breadcrumb. La IA no entiende la jerarquía.",
            fix="Implementa breadcrumbs visuales + Schema.org BreadcrumbList.", locked=True
        ))

    if not contact["has_contact_data"]:
        checks.append(CheckResult(
            name="Sin datos de contacto estructurados", icon="📇", pass_=False,
            fail_desc="No se encontró Schema.org Organization o LocalBusiness.",
            fix="Añade JSON-LD con @type: Organization incluyendo name, address, telephone...", locked=True
        ))

    if images["without_alt"] > 0 and images["total"] > 0:
        pct = (images["without_alt"] / images["total"]) * 100
        if pct > 20:
            checks.append(CheckResult(
                name="Imágenes sin alt text", icon="🖼️", pass_=False,
                fail_desc=f"{images['without_alt']} de {images['total']} imágenes ({pct:.0f}%) no tienen alt text.",
                fix="Añade atributo alt descriptivo a todas las imágenes.", locked=True
            ))

    if not meta["has_canonical"]:
        checks.append(CheckResult(
            name="URL canónica ausente", icon="🔗", pass_=False,
            fail_desc="Falta la etiqueta canonical. La IA no sabe cuál es la URL original.",
            fix="Añade: <link rel='canonical' href='URL-original-de-esta-pagina'>", locked=True
        ))

    if not meta["has_charset"]:
        checks.append(CheckResult(
            name="Charset no definido", icon="🔤", pass_=False,
            fail_desc="No se encontró meta charset. La IA puede interpretar mal caracteres especiales.",
            fix="Añade: <meta charset='UTF-8'> como primer elemento en <head>.", locked=True
        ))

    if links["empty"] > 5:
        checks.append(CheckResult(
            name="Enlaces vacíos o inválidos", icon="🔗", pass_=False,
            fail_desc=f"Hay {links['empty']} enlaces vacíos o con href='#'.",
            fix="Revisa todos los enlaces. Elimina los vacíos.", locked=True
        ))

    return checks


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest):
    start_time = time.time()
    url = request.url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    status_code, html, headers = await fetch_url(url)
    robots_ok, robots_content = await fetch_robots_txt(url)
    sitemap_ok, sitemap_count = await fetch_sitemap(url)

    checks = build_checks(url, status_code, html, robots_ok, sitemap_ok, sitemap_count)

    total_checks = 14
    failed_count = len(checks)
    passed_count = total_checks - failed_count
    score = max(0, min(100, int((passed_count / total_checks) * 100)))

    if score >= 80:
        status = "Excelente"
    elif score >= 60:
        status = "Necesita mejoras"
    else:
        status = "Poco visible para IA"

    analysis_time_ms = int((time.time() - start_time) * 1000)

    return AnalysisResponse(
        url=url, score=score, status=status,
        checks=[c.dict() for c in checks],
        failed_count=failed_count, passed_count=passed_count,
        analysis_time_ms=analysis_time_ms
    )


@app.get("/api/health")
async def health():
    return {"status": "ok"}
