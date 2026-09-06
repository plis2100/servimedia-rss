from __future__ import annotations

import email.utils
import html
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


BASE = "https://www.servimedia.es"

SECCIONES = [
    "/noticias/todas",
    "/noticias/economia",
    "/noticias/nacional",
    "/noticias/sociedad",
    "/noticias/autonomias",
    "/noticias/rsc",
    "/noticias/discapacidad",
    "/noticias/salud",
    "/noticias/mayores",
    "/noticias/tecnologia",
    "/noticias/medio-ambiente",
    "/noticias/tu-eres-europa",
    "/noticias/vacunate",
]

PAGINAS_POR_SECCION = 2
PAGINAS_TODAS = 4

SALIDA = Path("rss.xml")
ESTADO = Path("estado.json")

MAXIMO_ARTICULOS_RSS = 1500
MAXIMO_URL_ESTADO = 30000
MAXIMO_NUEVOS_POR_EJECUCION = 200
TRABAJADORES = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
)

CABECERAS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
    "Cache-Control": "no-cache",
}


def descargar(url: str, timeout: int = 60) -> bytes:
    peticion = urllib.request.Request(
        url,
        headers=CABECERAS,
    )

    with urllib.request.urlopen(
        peticion,
        timeout=timeout,
    ) as respuesta:
        contenido = respuesta.read()

    if not contenido:
        raise RuntimeError(f"Respuesta vacía: {url}")

    return contenido


def limpiar_url(url: str) -> str:
    url = urljoin(BASE, url)
    return url.split("#", 1)[0].split("?", 1)[0]


def es_articulo(url: str) -> bool:
    if not url.startswith(f"{BASE}/noticias/"):
        return False

    ruta = urlparse(url).path.rstrip("/")

    # Las noticias terminan con un identificador numérico.
    return bool(
        re.search(r"/noticias/.+/\d+$", ruta)
    )


def construir_paginas() -> list[str]:
    paginas: list[str] = [
        f"{BASE}/ultima-hora",
    ]

    for seccion in SECCIONES:
        cantidad = (
            PAGINAS_TODAS
            if seccion == "/noticias/todas"
            else PAGINAS_POR_SECCION
        )

        for numero in range(cantidad):
            if numero == 0:
                paginas.append(f"{BASE}{seccion}")
            else:
                paginas.append(
                    f"{BASE}{seccion}?page={numero}"
                )

    return paginas


def extraer_enlaces_pagina(url: str) -> set[str]:
    urls: set[str] = set()

    try:
        contenido = descargar(url)
        sopa = BeautifulSoup(contenido, "html.parser")

        for enlace in sopa.select("a[href]"):
            direccion = limpiar_url(
                enlace.get("href", "")
            )

            if es_articulo(direccion):
                urls.add(direccion)

    except Exception as error:
        print(f"No se pudo leer {url}: {error}")

    return urls


def localizar_articulos() -> set[str]:
    urls: set[str] = set()
    paginas = construir_paginas()

    with ThreadPoolExecutor(
        max_workers=TRABAJADORES
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                extraer_enlaces_pagina,
                pagina,
            ): pagina
            for pagina in paginas
        }

        for trabajo in as_completed(trabajos):
            urls.update(trabajo.result())

    return urls


def localizar_recientes() -> set[str]:
    paginas = [
        f"{BASE}/ultima-hora",
        f"{BASE}/noticias/todas",
        f"{BASE}/noticias/economia",
    ]

    urls: set[str] = set()

    with ThreadPoolExecutor(
        max_workers=3
    ) as ejecutor:
        trabajos = [
            ejecutor.submit(
                extraer_enlaces_pagina,
                pagina,
            )
            for pagina in paginas
        ]

        for trabajo in as_completed(trabajos):
            urls.update(trabajo.result())

    return urls


def cargar_estado() -> tuple[set[str], bool]:
    if not ESTADO.exists():
        return set(), True

    try:
        datos = json.loads(
            ESTADO.read_text(encoding="utf-8")
        )

        return set(datos.get("urls_vistas", [])), False

    except (json.JSONDecodeError, OSError):
        return set(), True


def guardar_texto_atomico(
    ruta: Path,
    contenido: str,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=".",
        prefix=f"{ruta.stem}_",
        suffix=".tmp",
    ) as temporal:
        temporal.write(contenido)
        ruta_temporal = Path(temporal.name)

    os.replace(ruta_temporal, ruta)


def guardar_estado(urls: set[str]) -> None:
    datos = {
        "ultima_actualizacion": datetime.now(
            timezone.utc
        ).isoformat(),
        "urls_vistas": sorted(urls)[-MAXIMO_URL_ESTADO:],
    }

    guardar_texto_atomico(
        ESTADO,
        json.dumps(
            datos,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )


def obtener_meta(
    sopa: BeautifulSoup,
    nombre: str,
    atributo: str = "property",
) -> str:
    etiqueta = sopa.find(
        "meta",
        attrs={atributo: nombre},
    )

    if not etiqueta:
        return ""

    return etiqueta.get("content", "").strip()


def convertir_fecha(fecha: str) -> datetime:
    if not fecha:
        return datetime.now(timezone.utc)

    fecha = fecha.strip().replace("Z", "+00:00")

    try:
        resultado = datetime.fromisoformat(fecha)

        if resultado.tzinfo is None:
            resultado = resultado.replace(
                tzinfo=timezone.utc
            )

        return resultado

    except ValueError:
        try:
            resultado = email.utils.parsedate_to_datetime(
                fecha
            )

            if resultado.tzinfo is None:
                resultado = resultado.replace(
                    tzinfo=timezone.utc
                )

            return resultado

        except (TypeError, ValueError):
            return datetime.now(timezone.utc)


def extraer_categoria(sopa: BeautifulSoup) -> str:
    # Subsección específica: Empresas, Transporte,
    # Banca, Energía, etc.
    subseccion = sopa.select_one(".srvm-nw__ptl p")

    if subseccion:
        texto = subseccion.get_text(
            " ",
            strip=True,
        )

        if texto:
            return texto

    # Sección principal: Economía, Sociedad, Nacional...
    seccion = sopa.select_one(".srvm_nw_scc a")

    if seccion:
        texto = seccion.get_text(
            " ",
            strip=True,
        )

        if texto:
            return texto

    palabras = obtener_meta(
        sopa,
        "keywords",
        "name",
    )

    if palabras:
        return palabras.split(",")[0].strip()

    return "Servimedia"


def extraer_articulo(url: str) -> dict | None:
    try:
        contenido = descargar(url, timeout=45)
        sopa = BeautifulSoup(contenido, "html.parser")

        titulo = obtener_meta(sopa, "og:title")

        if not titulo:
            encabezado = sopa.find("h1")

            if encabezado:
                titulo = encabezado.get_text(
                    " ",
                    strip=True,
                )

        descripcion = (
            obtener_meta(sopa, "description", "name")
            or obtener_meta(sopa, "og:description")
        )

        fecha = (
            obtener_meta(sopa, "og:published_time")
            or obtener_meta(
                sopa,
                "article:published_time",
            )
            or obtener_meta(
                sopa,
                "datePublished",
            )
        )

        imagen = obtener_meta(sopa, "og:image")

        autor = (
            obtener_meta(sopa, "author", "name")
            or obtener_meta(sopa, "article:author")
        )

        titulo = html.unescape(
            " ".join(titulo.split())
        )

        descripcion = html.unescape(
            " ".join(descripcion.split())
        )

        if not titulo:
            return None

        return {
            "url": url,
            "titulo": titulo,
            "descripcion": descripcion,
            "fecha": convertir_fecha(fecha),
            "categoria": extraer_categoria(sopa),
            "autor": autor,
            "imagen": imagen,
        }

    except Exception as error:
        print(f"No se pudo procesar {url}: {error}")
        return None


def texto_elemento(
    elemento: ET.Element,
    nombre: str,
) -> str:
    nodo = elemento.find(nombre)

    if nodo is None or nodo.text is None:
        return ""

    return nodo.text.strip()


def cargar_rss_anterior() -> dict[str, ET.Element]:
    articulos: dict[str, ET.Element] = {}

    if not SALIDA.exists():
        return articulos

    try:
        raiz = ET.parse(SALIDA).getroot()
        canal = raiz.find("channel")

        if canal is None:
            return articulos

        for item in canal.findall("item"):
            url = (
                texto_elemento(item, "guid")
                or texto_elemento(item, "link")
            )

            url = limpiar_url(url)

            if url:
                articulos[url] = item

    except ET.ParseError:
        print("El rss.xml anterior no era válido")

    return articulos


def fecha_item(item: ET.Element) -> datetime:
    return convertir_fecha(
        texto_elemento(item, "pubDate")
    )


def crear_item(datos: dict) -> ET.Element:
    item = ET.Element("item")

    ET.SubElement(item, "title").text = datos["titulo"]
    ET.SubElement(item, "link").text = datos["url"]

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "true"},
    )
    guid.text = datos["url"]

    ET.SubElement(item, "pubDate").text = (
        email.utils.format_datetime(datos["fecha"])
    )

    ET.SubElement(item, "category").text = (
        datos["categoria"]
    )

    if datos.get("descripcion"):
        ET.SubElement(item, "description").text = (
            datos["descripcion"]
        )

    if datos.get("autor"):
        ET.SubElement(item, "author").text = (
            datos["autor"]
        )

    if datos.get("imagen"):
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": datos["imagen"],
                "type": "image/jpeg",
            },
        )

    return item


def numero_articulo(url: str) -> int:
    coincidencia = re.search(r"/(\d+)/?$", url)

    if coincidencia:
        return int(coincidencia.group(1))

    return 0


def crear_rss(
    articulos: dict[str, ET.Element],
) -> ET.ElementTree:
    ordenados = sorted(
        articulos.values(),
        key=fecha_item,
        reverse=True,
    )[:MAXIMO_ARTICULOS_RSS]

    rss = ET.Element("rss", {"version": "2.0"})
    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = (
        "Servimedia — Todas las noticias"
    )

    ET.SubElement(canal, "link").text = BASE

    ET.SubElement(canal, "description").text = (
        "Todas las noticias públicas de Servimedia, "
        "incluidas las noticias de Empresas y Economía."
    )

    ET.SubElement(canal, "language").text = "es-ES"

    ET.SubElement(canal, "lastBuildDate").text = (
        email.utils.format_datetime(
            datetime.now(timezone.utc)
        )
    )

    for item in ordenados:
        canal.append(item)

    return ET.ElementTree(rss)


def guardar_xml_atomico(arbol: ET.ElementTree) -> None:
    ET.indent(arbol, space="  ")

    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=".",
        prefix="rss_",
        suffix=".xml",
    ) as temporal:
        ruta_temporal = Path(temporal.name)

        arbol.write(
            temporal,
            encoding="utf-8",
            xml_declaration=True,
        )

    os.replace(ruta_temporal, SALIDA)


def main() -> None:
    urls_localizadas = localizar_articulos()
    urls_recientes = localizar_recientes()

    urls_vistas, primera_ejecucion = cargar_estado()
    articulos = cargar_rss_anterior()

    if primera_ejecucion:
        # Registra todas las páginas localizadas, pero solo
        # añade las noticias recientes para no saturar Feedly.
        candidatos = urls_recientes
        urls_vistas.update(urls_localizadas)

        print(
            "Primera ejecución: se añadirán "
            f"{len(candidatos)} noticias recientes"
        )
    else:
        nuevas = urls_localizadas - urls_vistas

        # Los artículos recientes se vuelven a comprobar
        # si todavía no llegaron a incorporarse.
        candidatos = nuevas | urls_recientes

        nuevas_empresas = [
            url
            for url in nuevas
            if url not in articulos
        ]

        print(
            f"Nuevas URL detectadas: {len(nuevas)}"
        )

        print(
            "Candidatas de Economía/Empresas y resto: "
            f"{len(nuevas_empresas)}"
        )

    pendientes = [
        url
        for url in candidatos
        if url not in articulos
    ]

    pendientes.sort(
        key=numero_articulo,
        reverse=True,
    )

    pendientes = pendientes[:MAXIMO_NUEVOS_POR_EJECUCION]

    resultados: list[dict] = []

    with ThreadPoolExecutor(
        max_workers=TRABAJADORES
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                extraer_articulo,
                url,
            ): url
            for url in pendientes
        }

        for trabajo in as_completed(trabajos):
            resultado = trabajo.result()

            if resultado:
                resultados.append(resultado)

    for resultado in resultados:
        articulos[resultado["url"]] = crear_item(resultado)
        urls_vistas.add(resultado["url"])

    # También registra los artículos que ya estaban guardados.
    urls_vistas.update(articulos.keys())

    guardar_xml_atomico(
        crear_rss(articulos)
    )

    guardar_estado(urls_vistas)

    empresas = sum(
        1
        for resultado in resultados
        if resultado["categoria"].lower() == "empresas"
    )

    print(
        f"RSS actualizado: {len(articulos)} noticias"
    )

    print(
        f"Nuevas añadidas: {len(resultados)}"
    )

    print(
        f"Nuevas clasificadas como Empresas: {empresas}"
    )


if __name__ == "__main__":
    main()
