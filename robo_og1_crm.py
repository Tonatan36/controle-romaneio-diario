import os
import re
import time
import shutil
import requests
import pdfplumber
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ================= CONFIGURAÇÕES =================
PASTA_MONITORADA = os.path.join(os.path.expanduser("~"), "Downloads")
MOVER_APOS_LER = False

SUPABASE_REST_URL = "https://iqfcyxuhqnluucifylqn.supabase.co/rest/v1"
SUPABASE_API_KEY = "sb_publishable_PSTBZr3SnTxlmCqxFth3Pw_3LLr38Gb"
VENDEDOR_PADRAO = "Ewerton"

HEADERS = {
    "apikey": SUPABASE_API_KEY,
    "Authorization": f"Bearer {SUPABASE_API_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal"
}


def extrair_valor_monetario(texto):
    limpo = re.sub(r"[^\d,]", "", str(texto)).replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return 0.0


def classificar_categoria(texto):
    txt = texto.upper()
    if re.search(r"TELHA|CUMIEIRA|GALVALUME|TRAPEZOIDAL", txt):
        return "TELHA"
    if re.search(r"FORRO|PVC|RODAFORRO", txt):
        return "FORRO PVC"
    if re.search(r"POSTE|ENERGISA|CAIXA MEDIDOR", txt):
        return "POSTE ENERGISA"
    if re.search(r"ALUMINIO|ALUMÍNIO|LAMBRI|PERFIL AL", txt):
        return "ALUMÍNIO"
    if re.search(r"PARAFUSO|BROCA|DISCO|ELETRODO|FECHADURA|DOBRADICA|ARAME|PREGO", txt):
        return "FERRAGENS"
    return "AÇO / PERFIS"


def mapear_vendedor(nome_og1):
    n = str(nome_og1).upper()
    if "EWERTON" in n:
        return "Ewerton"
    if "ALEXANDRE" in n:
        return "Alexandre"
    if "PAULO" in n:
        return "Paulo"
    if "YAN" in n:
        return "Yan"
    return VENDEDOR_PADRAO


def extrair_dados_pdf_og1(caminho_pdf):
    texto_completo = ""
    with pdfplumber.open(caminho_pdf) as pdf:
        for pagina in pdf.pages:
            t = pagina.extract_text()
            if t:
                texto_completo += t + "\n"

    upper = texto_completo.upper()
    if "ACO FORTE" not in upper and "AÇO FORTE" not in upper:
        return None

    linhas = [l.strip() for l in texto_completo.split("\n") if l.strip()]

    is_pedido = bool(re.search(r"PEDIDO\s+DE\s+VENDA", upper)) and not bool(
        re.search(r"OR[ÇC]AMENTO", upper[:400])
    )
    m_doc = re.search(r"N[º°]\.\s*:\s*(\d{4,8})", upper) or re.search(
        r"(?:OR[ÇC]AMENTO|PEDIDO)\s*(?:N[º°.:]*)?\s*(\d{4,8})", upper
    )
    numero_doc = m_doc.group(1) if m_doc else ""

    codigo_cliente = ""
    nome_cliente = ""
    for idx, linha in enumerate(linhas):
        if re.match(r"^CLIENTE[\s.:]+", linha, re.IGNORECASE):
            m_cod = re.search(r"CLIENTE[\s.:]+(\d{1,8})", linha, re.IGNORECASE)
            if m_cod:
                codigo_cliente = m_cod.group(1)
            resto = re.sub(r"^CLIENTE[\s.:]+\d{1,8}\s*[-–]?\s*", "", linha, flags=re.IGNORECASE)
            resto = re.sub(r"\s*N[º°].*$", "", resto, flags=re.IGNORECASE).strip()
            if resto and not re.match(r"^(N[º°]|DATA|HORA)", resto, re.IGNORECASE):
                nome_cliente = resto.upper()
            elif idx + 1 < len(linhas):
                nome_cliente = re.sub(
                    r"\s*(?:N[º°]\.?|DATA\.|HORA:).*$", "", linhas[idx + 1], flags=re.IGNORECASE
                ).strip().upper()
            break

    telefone = ""
    m_tel = re.search(r"(?:FONE1|CELULAR)[\s.:]+0?(\d{2})[-\s]?(\d{4,5}[-\s]?\d{4})", texto_completo, re.IGNORECASE)
    if m_tel:
        telefone = m_tel.group(1) + re.sub(r"\D", "", m_tel.group(2))

    vendedor = VENDEDOR_PADRAO
    m_user = re.search(r"Usu[áa]rio\s*:\s*([A-ZÀ-Ú]+)", texto_completo, re.IGNORECASE)
    if m_user:
        vendedor = mapear_vendedor(m_user.group(1))

    m_val = re.search(r"TOTAL\s+LIQUIDO[\s.|]*([\d.]+,\d{2})", texto_completo, re.IGNORECASE) or re.search(
        r"TOTAL\s+BRUTO[\s.|]*([\d.]+,\d{2})", texto_completo, re.IGNORECASE
    )
    valor_total = extrair_valor_monetario(m_val.group(1)) if m_val else 0.0

    itens = []
    for l in linhas:
        if re.match(r"^\d{9}\b", l):
            limpo = re.sub(r"^\d{9}\s*\|?\s*", "", l)
            limpo = re.sub(r"\s*\|\s*", " ", limpo)
            itens.append(re.sub(r"\s+", " ", limpo).strip())

    itens_texto = "\n".join(itens)
    resumo = " | ".join(itens[:3])[:140]
    categoria = classificar_categoria(itens_texto or texto_completo)

    return {
        "is_pedido": is_pedido,
        "numero_doc": numero_doc,
        "codigo_cliente": codigo_cliente,
        "nome_cliente": nome_cliente or "CLIENTE PDF OG1",
        "telefone": telefone,
        "vendedor": vendedor,
        "valor_total": valor_total,
        "categoria": categoria,
        "itens_texto": itens_texto,
        "resumo": resumo
    }


def enviar_para_supabase(dados, nome_arquivo):
    hoje_iso = datetime.now().strftime("%Y-%m-%d")
    agora_br = datetime.now().strftime("%d/%m/%Y %H:%M")

    if dados["codigo_cliente"].isdigit() and dados["nome_cliente"]:
        try:
            requests.post(
                f"{SUPABASE_REST_URL}/clientes_acoforte?on_conflict=codigo",
                headers={**HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                json=[{
                    "codigo": int(dados["codigo_cliente"]),
                    "nome": dados["nome_cliente"],
                    "telefone": dados["telefone"] or None
                }],
                timeout=10
            )
        except Exception:
            pass

    tag_doc = f"PEDIDO OG1 #{dados['numero_doc']}" if dados["is_pedido"] else f"ORÇ. OG1 #{dados['numero_doc']}"
    obs = f"{tag_doc} — {dados['resumo']}" if dados["resumo"] else tag_doc

    registro_crm = {
        "id": int(time.time() * 1000),
        "data": hoje_iso,
        "vendedor": dados["vendedor"],
        "codigo_cliente": dados["codigo_cliente"],
        "cliente": dados["nome_cliente"],
        "telefone": dados["telefone"],
        "categoria": dados["categoria"],
        "etapa_kanban": "FECHAMENTO" if dados["is_pedido"] else "PROPOSTA",
        "temperatura": "QUENTE" if dados["is_pedido"] else "MORNO",
        "tipo_saida": "RETIRA BALCÃO",
        "motivo_perda": "",
        "valor": dados["valor_total"],
        "observacao": obs,
        "notas_timeline": [{
            "autor": f"Robô OG1 ({dados['vendedor']})",
            "dataHora": agora_br,
            "texto": f"Importado automaticamente de {nome_arquivo}:\n{dados['itens_texto']}"
        }],
        "romaneio_id": None
    }

    resp = requests.post(
        f"{SUPABASE_REST_URL}/vendas_crm?on_conflict=id",
        headers=HEADERS,
        json=[registro_crm],
        timeout=10
    )
    resp.raise_for_status()
    print(f"✅ [{agora_br}] Sincronizado: {tag_doc} | {dados['nome_cliente']} | R$ {dados['valor_total']:.2f} ({dados['vendedor']})")


class ObservadorOG1(FileSystemEventHandler):
    def __init__(self):
        self.processados = set()

    def on_created(self, event):
        if event.is_directory or not event.src_path.lower().endswith(".pdf"):
            return
        caminho = event.src_path
        if caminho in self.processados:
            return
        self.processados.add(caminho)

        time.sleep(1.5)
        try:
            dados = extrair_dados_pdf_og1(caminho)
            if not dados:
                return

            enviar_para_supabase(dados, os.path.basename(caminho))

            if MOVER_APOS_LER:
                destino_dir = os.path.join(PASTA_MONITORADA, "Processados_CRM")
                os.makedirs(destino_dir, exist_ok=True)
                shutil.move(caminho, os.path.join(destino_dir, os.path.basename(caminho)))
        except Exception as e:
            print(f"⚠️ Erro ao processar {os.path.basename(caminho)}: {e}")


if __name__ == "__main__":
    print(f"🤖 Robô Observador OG1 -> CRM Aço Forte ativo!")
    print(f"📂 Vigiando pasta: {PASTA_MONITORADA}")
    print("Pressione Ctrl+C para encerrar.\n")

    handler = ObservadorOG1()
    observer = Observer()
    observer.schedule(handler, PASTA_MONITORADA, recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()