# -*- coding: utf-8 -*-
"""
Script QGIS Processing: SAPO - Verificar Ligação Entre Bancos
Versão: 1.0.0
Grupo: SAPO
Compatibilidade: QGIS 3.24+

Pipeline de Verificação e Validação de Ligação Entre Bancos de Dados:
1. Permite selecionar 2 ou mais camadas de moldura (apenas camadas de área ou linha com elementos).
2. Identifica conexões de Linhas e Áreas na divisa mútua (tocam em pelo menos 2 molduras).
3. Analisa no raio de 1 metro se existe feição correspondente da mesma classe no banco vizinho.
4. Compara rigorosamente os atributos (ignorando metadados '_', id, observação, datas, operadores e dimensões).
5. Categoriza e gera camadas organizadas no grupo 'Sapo - Validação de Ligação entre Bancos (1m)':
   - 🟢 Ligacoes_Validadas_OK
   - 🟡 Erros_Atributos_Divergentes (com detalhamento das divergências)
   - 🔴 Erros_Sem_Ligacao (pontas soltas)
"""

import os
import re
from pathlib import Path

from PyQt5.QtCore import Qt, QCoreApplication, QVariant
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
    QLineEdit, QGroupBox, QDoubleSpinBox, QProgressBar, QMessageBox,
    QApplication, QWidget, QAbstractItemView
)
from PyQt5.QtGui import QColor, QFont

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber,
    QgsProcessingException,
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsDistanceArea,
    QgsCoordinateTransformContext
)
from qgis.utils import iface


# =============================================================================
# CONSTANTES DE CAMPOS IGNORADOS NA COMPARAÇÃO DE ATRIBUTOS
# =============================================================================

CAMPOS_IGNORADOS = {
    'id', 'fid', 'gid', 'observacao', 'observacoes',
    'operador_criacao', 'data_criacao',
    'operador_atualizacao', 'data_atualizacao',
    'length_otf', 'lenght_otf', 'area_otf'
}


def campo_deve_ignorar(nome_campo):
    """
    Retorna True se o campo deve ser ignorado na validação de ligação:
    - Inicia com sublinhado '_' (metadados do processo)
    - Consta na lista de campos ignorados (id, observacao, datas, operadores, length_otf, area_otf)
    """
    nome = nome_campo.lower().strip()
    return nome.startswith('_') or nome in CAMPOS_IGNORADOS


def valores_sao_iguais(v1, v2):
    """
    Compara dois valores de atributos considerando equivalência de nulos e precisão numérica.
    """
    nulo1 = v1 is None or v1 == QVariant() or str(v1).strip() in ('', 'NULL', 'None')
    nulo2 = v2 is None or v2 == QVariant() or str(v2).strip() in ('', 'NULL', 'None')

    if nulo1 and nulo2:
        return True
    if nulo1 != nulo2:
        return False

    try:
        f1 = float(v1)
        f2 = float(v2)
        return abs(f1 - f2) < 1e-4
    except (ValueError, TypeError):
        pass

    return str(v1).strip() == str(v2).strip()


def comparar_atributos(dict_a1, dict_a2):
    """
    Compara os atributos de duas feições ignorando campos de controle e metadados.
    Retorna uma lista de strings descrevendo cada diferença encontrada.
    """
    diferencas = []
    chaves_todas = set(dict_a1.keys()).union(set(dict_a2.keys()))

    for k in sorted(chaves_todas):
        if campo_deve_ignorar(k):
            continue

        v1 = dict_a1.get(k)
        v2 = dict_a2.get(k)

        if not valores_sao_iguais(v1, v2):
            diferencas.append(f"{k} ('{v1}' != '{v2}')")

    return diferencas


# =============================================================================
# FUNÇÕES DE GEOMETRIA, MEDIÇÃO E INSPEÇÃO
# =============================================================================

def obter_medidor_distancia(crs):
    """Cria um objeto QgsDistanceArea para medir distâncias geodésicas exatas em metros."""
    da = QgsDistanceArea()
    context = QgsProject.instance().transformContext()
    da.setSourceCrs(crs, context)
    ellipsoid = QgsProject.instance().ellipsoid() or 'GRS80'
    da.setEllipsoid(ellipsoid)
    return da


def calcular_tolerancia_unidade_camada(crs, distancia_metros=1.0):
    """
    Converte uma tolerância dada em metros para a unidade da camada (graus se geográfica).
    1 metro no equador ~ 1 / 111320 graus.
    """
    if crs.isGeographic():
        return distancia_metros / 111320.0
    return distancia_metros


def camada_tem_elementos(camada):
    """Verifica se a camada contém ao menos 1 elemento."""
    try:
        count = camada.featureCount()
        if count > 0:
            return True
        if count == 0:
            return False
        for _ in camada.getFeatures(QgsFeatureRequest().setLimit(1)):
            return True
        return False
    except Exception:
        return False


def obter_quantidade_elementos(camada):
    """Retorna o total de elementos da camada."""
    count = camada.featureCount()
    if count >= 0:
        return count
    c = 0
    for _ in camada.getFeatures():
        c += 1
    return c


def obter_info_grupos(camada):
    """Retorna (grupo_pai, caminho_completo)."""
    root = QgsProject.instance().layerTreeRoot()
    node = root.findLayer(camada.id())
    if not node:
        return "root", "root"

    path_nodes = []
    curr = node.parent()
    while curr is not None and curr != root:
        path_nodes.insert(0, curr.name())
        curr = curr.parent()

    if not path_nodes:
        return "root", "root"

    grupo_pai = path_nodes[0]
    caminho_completo = " / ".join(path_nodes)
    return grupo_pai, caminho_completo


def obter_nome_banco_dados(camada):
    """Identifica o nome do banco de dados de origem (PostGIS, GPKG, etc.)."""
    try:
        source = camada.source()
        provider = camada.providerType()

        if provider == 'postgres':
            uri = QgsDataSourceUri(source)
            db = uri.database()
            if db:
                host = uri.host()
                return f"{db} ({host})" if host else db
            m = re.search(r"dbname=['\"]?([^'\"\s]+)['\"]?", source)
            if m:
                return m.group(1)

        clean_path = source.split('|')[0]
        exts = ('.gpkg', '.sqlite', '.db', '.shp', '.geojson')
        if any(clean_path.lower().endswith(ext) for ext in exts):
            return os.path.basename(clean_path)

        uri = QgsDataSourceUri(source)
        if uri.database():
            return uri.database()

        if provider == 'memory':
            return "Memória"

        return "N/A"
    except Exception:
        return "N/A"


def extrair_contorno_poligono(geom):
    """Retorna o contorno linear de um polígono de forma compatível com PyQGIS."""
    if not geom or geom.isEmpty():
        return None

    try:
        abstract_geom = geom.constGet()
        if abstract_geom is not None:
            b = abstract_geom.boundary()
            if b is not None:
                return QgsGeometry(b)
    except Exception:
        pass

    try:
        linhas = []
        if geom.isMultipart():
            for poly in geom.asMultiPolygon():
                for anel in poly:
                    if len(anel) >= 2:
                        linhas.append(QgsGeometry.fromPolylineXY(anel))
        else:
            for anel in geom.asPolygon():
                if len(anel) >= 2:
                    linhas.append(QgsGeometry.fromPolylineXY(anel))
        if linhas:
            return QgsGeometry.unaryUnion(linhas) if len(linhas) > 1 else linhas[0]
    except Exception:
        pass

    return None


def extrair_geometria_fronteira(camada_moldura, crs_destino):
    """Converte a moldura em linhas unificadas."""
    transform = QgsCoordinateTransform(camada_moldura.crs(), crs_destino, QgsProject.instance())
    geometrias = []

    for feat in camada_moldura.getFeatures():
        geom = feat.geometry()
        if not geom or geom.isEmpty():
            continue

        g = QgsGeometry(geom)
        if camada_moldura.crs() != crs_destino:
            g.transform(transform)

        tipo = g.type()
        if tipo == QgsWkbTypes.PolygonGeometry:
            fronteira = extrair_contorno_poligono(g)
            if fronteira and not fronteira.isEmpty():
                geometrias.append(fronteira)
        elif tipo == QgsWkbTypes.LineGeometry:
            geometrias.append(g)

    if not geometrias:
        return None
    if len(geometrias) == 1:
        return geometrias[0]
    return QgsGeometry.unaryUnion(geometrias)


def extrair_geometria_completa(camada_moldura, crs_destino):
    """Retorna a geometria completa unificada da moldura."""
    transform = QgsCoordinateTransform(camada_moldura.crs(), crs_destino, QgsProject.instance())
    geometrias = []

    for feat in camada_moldura.getFeatures():
        geom = feat.geometry()
        if not geom or geom.isEmpty():
            continue

        g = QgsGeometry(geom)
        if camada_moldura.crs() != crs_destino:
            g.transform(transform)
        geometrias.append(g)

    if not geometrias:
        return None
    if len(geometrias) == 1:
        return geometrias[0]
    return QgsGeometry.unaryUnion(geometrias)


def extrair_pontos_de_geometria(geom):
    """Extrai pontos e vértices de qualquer geometria."""
    pontos = []
    if geom is None or geom.isEmpty():
        return pontos

    tipo = geom.type()
    wkb_type = geom.wkbType()

    if tipo == QgsWkbTypes.PointGeometry:
        if QgsWkbTypes.isMultiType(wkb_type):
            for pt in geom.asMultiPoint():
                pontos.append(pt)
        else:
            pontos.append(geom.asPoint())
    elif tipo == QgsWkbTypes.LineGeometry:
        if QgsWkbTypes.isMultiType(wkb_type):
            for polyline in geom.asMultiPolyline():
                for pt in polyline:
                    pontos.append(pt)
        else:
            for pt in geom.asPolyline():
                pontos.append(pt)
    elif geom.isMultipart():
        for parte in geom.asGeometryCollection():
            pontos.extend(extrair_pontos_de_geometria(parte))
    return pontos


def coletar_pontos_conexao_feicao(geom_feicao, tipo_geom, geom_fronteira, tolerancia):
    """Identifica os pontos de toque ou interseção de uma feição com a fronteira da moldura."""
    pontos_encontrados = []

    if tipo_geom == QgsWkbTypes.LineGeometry:
        inter = geom_feicao.intersection(geom_fronteira)
        if inter and not inter.isEmpty():
            pontos_encontrados.extend(extrair_pontos_de_geometria(inter))

        pt_ini = geom_feicao.interpolate(0.0)
        pt_fim = geom_feicao.interpolate(geom_feicao.length())
        if pt_ini and pt_ini.distance(geom_fronteira) <= tolerancia:
            pontos_encontrados.append(pt_ini.asPoint())
        if pt_fim and pt_fim.distance(geom_fronteira) <= tolerancia:
            pontos_encontrados.append(pt_fim.asPoint())

    elif tipo_geom == QgsWkbTypes.PolygonGeometry:
        if geom_feicao.intersects(geom_fronteira) or geom_feicao.distance(geom_fronteira) <= tolerancia:
            contorno = extrair_contorno_poligono(geom_feicao)
            if contorno and not contorno.isEmpty():
                inter_contorno = contorno.intersection(geom_fronteira)
                if inter_contorno and not inter_contorno.isEmpty():
                    pontos_encontrados.extend(extrair_pontos_de_geometria(inter_contorno))

            for pt_vert in geom_feicao.vertices():
                pt_geom = QgsGeometry.fromPointXY(QgsPointXY(pt_vert.x(), pt_vert.y()))
                if pt_geom.distance(geom_fronteira) <= tolerancia:
                    pontos_encontrados.append(QgsPointXY(pt_vert.x(), pt_vert.y()))

    pontos_unicos = []
    tol_duplicado = tolerancia * 0.1 if tolerancia > 0 else 1e-6
    for pt in pontos_encontrados:
        duplicado = False
        for pu in pontos_unicos:
            if abs(pt.x() - pu.x()) <= tol_duplicado and abs(pt.y() - pu.y()) <= tol_duplicado:
                duplicado = True
                break
        if not duplicado:
            pontos_unicos.append(pt)

    return pontos_unicos


# =============================================================================
# PIPELINE COMPLETO DE GERAÇÃO E VALIDAÇÃO DE LIGAÇÕES ENTRE BANCOS
# =============================================================================

def executar_pipeline_validacao_ligacao(camadas_moldura, incluir_linhas=True, incluir_areas=True, tolerancia_metros=1.0, callback_progresso=None):
    """
    Pipeline unificado de validação:
    1. Para cada banco, gera os pontos na moldura que tocam em pelo menos 2 molduras selecionadas.
    2. Analisa cada ponto em relação ao banco vizinho:
       - Deve existir um ponto correspondente no mesmo local (distância <= tolerancia_metros, padrão 1m).
       - Deve ser da mesma classe/camada (mesmo tipo).
       - Compara todos os atributos (exceto campos ignorados e metadados com '_').
    3. Gera camadas de resultados no QGIS:
       - 🟢 Ligacoes_Validadas_OK
       - 🟡 Erros_Atributos_Divergentes
       - 🔴 Erros_Sem_Ligacao
    """
    if len(camadas_moldura) < 2:
        raise ValueError("É obrigatório selecionar pelo menos 2 camadas de moldura!")

    projeto = QgsProject.instance()

    # Prepara metadados das molduras
    info_molduras = []
    for m in camadas_moldura:
        crs_m = m.crs()
        tol_local = calcular_tolerancia_unidade_camada(crs_m, distancia_metros=tolerancia_metros)
        geom_fronteira = extrair_geometria_fronteira(m, crs_m)
        geom_completa = extrair_geometria_completa(m, crs_m)
        grupo_pai, _ = obter_info_grupos(m)
        nome_id = f"{grupo_pai}_{m.name()}" if grupo_pai != "root" else m.name()

        info_molduras.append({
            'layer': m,
            'layer_id': m.id(),
            'nome_id': nome_id,
            'grupo_pai': grupo_pai,
            'crs': crs_m,
            'fronteira': geom_fronteira,
            'completa': geom_completa,
            'tol_unidade': tol_local,
            'medidor': obter_medidor_distancia(crs_m)
        })

    # Coleta camadas vetoriais de interesse (Linhas e Áreas não vazias)
    ids_molduras = {m.id() for m in camadas_moldura}
    tipos_aceitos = []
    if incluir_linhas:
        tipos_aceitos.append(QgsWkbTypes.LineGeometry)
    if incluir_areas:
        tipos_aceitos.append(QgsWkbTypes.PolygonGeometry)

    camadas_alvo = []
    for layer in projeto.mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            if layer.id() not in ids_molduras and layer.geometryType() in tipos_aceitos:
                if camada_tem_elementos(layer):
                    camadas_alvo.append(layer)

    if not camadas_alvo:
        raise ValueError("Nenhuma camada de Linhas ou Áreas com elementos encontrada para análise.")

    # -------------------------------------------------------------------------
    # ETAPA 1: GERAÇÃO DE PONTOS DE CONEXÃO NAS MOLDURAS
    # -------------------------------------------------------------------------
    if callback_progresso:
        callback_progresso(10, "Etapa 1: Gerando pontos de conexão na moldura de cada banco...")

    todos_pontos_conexao = []
    total_m = len(info_molduras)

    for idx_m, dados_m1 in enumerate(info_molduras):
        m1_layer = dados_m1['layer']
        m1_id = dados_m1['layer_id']
        nome_m1 = dados_m1['nome_id']
        grupo_m1 = dados_m1['grupo_pai']
        crs_m1 = dados_m1['crs']
        gf_m1 = dados_m1['fronteira']
        gc_m1 = dados_m1['completa']
        tol_m1 = dados_m1['tol_unidade']

        if not gf_m1 or gf_m1.isEmpty():
            continue

        for camada_alvo in camadas_alvo:
            grupo_alvo, _ = obter_info_grupos(camada_alvo)
            if grupo_alvo != "root" and grupo_m1 != "root" and grupo_alvo != grupo_m1:
                continue

            tipo_geom = camada_alvo.geometryType()
            str_tipo = "Linha" if tipo_geom == QgsWkbTypes.LineGeometry else "Área"
            nome_base_camada = camada_alvo.name().split('.')[-1].lower()

            transform_alvo_m1 = None
            if camada_alvo.crs() != crs_m1:
                transform_alvo_m1 = QgsCoordinateTransform(camada_alvo.crs(), crs_m1, projeto)

            for feat in camada_alvo.getFeatures():
                geom = feat.geometry()
                if not geom or geom.isEmpty():
                    continue

                geom_tr = QgsGeometry(geom)
                if transform_alvo_m1:
                    geom_tr.transform(transform_alvo_m1)

                if gc_m1 and not geom_tr.intersects(gc_m1) and gc_m1.distance(geom_tr) > tol_m1:
                    continue

                pts_brutos = coletar_pontos_conexao_feicao(geom_tr, tipo_geom, gf_m1, tol_m1)

                for pt in pts_brutos:
                    pt_geom_m1 = QgsGeometry.fromPointXY(pt)

                    bancos_tocados = [nome_m1]
                    for dados_m2 in info_molduras:
                        if dados_m2['layer_id'] == m1_id:
                            continue

                        pt_geom_m2 = QgsGeometry(pt_geom_m1)
                        if crs_m1 != dados_m2['crs']:
                            tr = QgsCoordinateTransform(crs_m1, dados_m2['crs'], projeto)
                            pt_geom_m2.transform(tr)

                        tol_m2 = dados_m2['tol_unidade']
                        gf2 = dados_m2['fronteira']
                        gc2 = dados_m2['completa']

                        toca = False
                        if gf2 and pt_geom_m2.distance(gf2) <= tol_m2:
                            toca = True
                        elif gc2 and pt_geom_m2.distance(gc2) <= tol_m2:
                            toca = True

                        if toca:
                            bancos_tocados.append(dados_m2['nome_id'])

                    if len(bancos_tocados) < 2:
                        continue

                    atributos_originais = {}
                    for fld in camada_alvo.fields():
                        atributos_originais[fld.name()] = feat[fld.name()]

                    todos_pontos_conexao.append({
                        'moldura_id': m1_id,
                        'banco_origem': nome_m1,
                        'grupo_pai': grupo_m1,
                        'camada_nome': camada_alvo.name(),
                        'camada_base': nome_base_camada,
                        'tipo_geom': str_tipo,
                        'id_origem': feat.id(),
                        'pt': pt,
                        'pt_geom': pt_geom_m1,
                        'crs': crs_m1,
                        'bancos_conectados': bancos_tocados,
                        'atributos': atributos_originais
                    })

    if not todos_pontos_conexao:
        raise ValueError(
            "Nenhum ponto de conexão foi encontrado na fronteira entre as molduras selecionadas. "
            "Verifique se as feições realmente tocam a divisa mútua entre os bancos."
        )

    # -------------------------------------------------------------------------
    # ETAPA 2: ANÁLISE DE LIGAÇÕES MÚTUAS E VALIDAÇÃO DE ATRIBUTOS (<= 1 METRO)
    # -------------------------------------------------------------------------
    if callback_progresso:
        callback_progresso(50, "Etapa 2: Analisando pares de ligação e comparando atributos...")

    crs_ref = info_molduras[0]['crs']
    medidor_ref = obter_medidor_distancia(crs_ref)

    for p_info in todos_pontos_conexao:
        if p_info['crs'] != crs_ref:
            tr = QgsCoordinateTransform(p_info['crs'], crs_ref, projeto)
            g_ref = QgsGeometry(p_info['pt_geom'])
            g_ref.transform(tr)
            p_info['pt_geom_ref'] = g_ref
            p_info['pt_ref'] = g_ref.asPoint()
        else:
            p_info['pt_geom_ref'] = p_info['pt_geom']
            p_info['pt_ref'] = p_info['pt']

    tol_busca_ref = calcular_tolerancia_unidade_camada(crs_ref, distancia_metros=tolerancia_metros * 1.5)

    indice_espacial = QgsSpatialIndex()
    for idx, p_info in enumerate(todos_pontos_conexao):
        feat_temp = QgsFeature(idx)
        feat_temp.setGeometry(p_info['pt_geom_ref'])
        indice_espacial.addFeature(feat_temp)

    resultados_ok = []
    resultados_divergentes = []
    resultados_sem_ligacao = []

    for idx_p1, p1 in enumerate(todos_pontos_conexao):
        pt1_ref = p1['pt_ref']
        bbox_busca = QgsGeometry.fromPointXY(pt1_ref).buffer(tol_busca_ref, 4).boundingBox()
        candidatos_ids = indice_espacial.intersects(bbox_busca)

        melhor_candidato = None
        menor_dist_metros = float('inf')

        for c_id in candidatos_ids:
            if c_id == idx_p1:
                continue
            p2 = todos_pontos_conexao[c_id]

            if p2['moldura_id'] == p1['moldura_id']:
                continue

            if p2['camada_base'] != p1['camada_base']:
                continue

            dist_m = medidor_ref.measureLine(pt1_ref, p2['pt_ref'])
            if dist_m <= tolerancia_metros:
                if dist_m < menor_dist_metros:
                    menor_dist_metros = dist_m
                    melhor_candidato = p2

        if melhor_candidato is not None:
            difs = comparar_atributos(p1['atributos'], melhor_candidato['atributos'])

            registro = {
                'ponto': p1,
                'ponto_par': melhor_candidato,
                'distancia_m': menor_dist_metros,
                'banco_origem': p1['banco_origem'],
                'banco_vizinho': melhor_candidato['banco_origem'],
                'camada': p1['camada_nome'],
                'tipo_geom': p1['tipo_geom'],
                'divergencias': "; ".join(difs) if difs else ""
            }

            if not difs:
                resultados_ok.append(registro)
            else:
                resultados_divergentes.append(registro)
        else:
            registro = {
                'ponto': p1,
                'ponto_par': None,
                'distancia_m': -1.0,
                'banco_origem': p1['banco_origem'],
                'banco_vizinho': "NENHUM (Não encontrado)",
                'camada': p1['camada_nome'],
                'tipo_geom': p1['tipo_geom'],
                'divergencias': "Ponta solta: sem elemento correspondente do outro banco no raio de 1 metro."
            }
            resultados_sem_ligacao.append(registro)

    # -------------------------------------------------------------------------
    # ETAPA 3: CRIAÇÃO DAS CAMADAS DE RESULTADO NO QGIS
    # -------------------------------------------------------------------------
    if callback_progresso:
        callback_progresso(80, "Etapa 3: Criando camadas de validação e relatório...")

    root = projeto.layerTreeRoot()
    nome_grupo_resultado = "Sapo - Validação de Ligação entre Bancos (1m)"
    grupo_resultado = root.findGroup(nome_grupo_resultado)
    if not grupo_resultado:
        grupo_resultado = root.insertGroup(0, nome_grupo_resultado)

    def criar_camada_resultado(nome_layer, registros):
        if not registros:
            return None

        uri = f"Point?crs={crs_ref.authid()}"
        layer = QgsVectorLayer(uri, nome_layer, "memory")
        pr = layer.dataProvider()

        campos = QgsFields()
        campos.append(QgsField("_banco_origem", QVariant.String, len=100))
        campos.append(QgsField("_banco_vizinho", QVariant.String, len=100))
        campos.append(QgsField("_camada_origem", QVariant.String, len=100))
        campos.append(QgsField("_tipo_geometria", QVariant.String, len=20))
        campos.append(QgsField("_distancia_m", QVariant.Double))
        campos.append(QgsField("_divergencias", QVariant.String, len=500))

        todos_campos = set()
        for reg in registros:
            for k in reg['ponto']['atributos'].keys():
                if not campo_deve_ignorar(k):
                    todos_campos.add(k)

        for k in sorted(todos_campos):
            campos.append(QgsField(k, QVariant.String, len=255))

        pr.addAttributes(campos)
        layer.updateFields()

        nomes_campos_validos = set(layer.fields().names())

        feats = []
        for reg in registros:
            p = reg['ponto']
            f = QgsFeature(layer.fields())
            f.setGeometry(p['pt_geom_ref'])

            f.setAttribute("_banco_origem", reg['banco_origem'])
            f.setAttribute("_banco_vizinho", reg['banco_vizinho'])
            f.setAttribute("_camada_origem", reg['camada'])
            f.setAttribute("_tipo_geometria", reg['tipo_geom'])
            f.setAttribute("_distancia_m", round(reg['distancia_m'], 3) if reg['distancia_m'] >= 0 else None)
            f.setAttribute("_divergencias", reg['divergencias'])

            for k, val in p['atributos'].items():
                if k in nomes_campos_validos:
                    f.setAttribute(k, str(val) if val is not None else "")

            feats.append(f)

        pr.addFeatures(feats)
        layer.updateExtents()
        projeto.addMapLayer(layer, False)
        grupo_resultado.addLayer(layer)
        return layer

    criar_camada_resultado("🔴 Erros_Sem_Ligacao", resultados_sem_ligacao)
    criar_camada_resultado("🟡 Erros_Atributos_Divergentes", resultados_divergentes)
    criar_camada_resultado("🟢 Ligacoes_Validadas_OK", resultados_ok)

    resumo_final = {
        'total_analisado': len(todos_pontos_conexao),
        'total_ok': len(resultados_ok),
        'total_divergentes': len(resultados_divergentes),
        'total_sem_ligacao': len(resultados_sem_ligacao),
        'exemplos_divergencias': [r['divergencias'] for r in resultados_divergentes[:10]],
        'tolerancia_m': tolerancia_metros
    }

    if callback_progresso:
        callback_progresso(100, "Validação concluída com sucesso!")

    return resumo_final


# =============================================================================
# INTERFACE GRÁFICA PERSONALIZADA (PYQT5)
# =============================================================================

class DialogoSapoVerificarLigacao(QDialog):
    """
    Diálogo SAPO com listagem estrita de molduras não vazias.
    Executa a verificação completa de ligação a 1m de tolerância com comparação de atributos.
    """

    VERSAO = "1.0.0"

    def __init__(self, parent=None):
        super().__init__(parent or (iface.mainWindow() if iface else None))
        self.setWindowTitle(f"Verificar Ligação Entre Bancos v{self.VERSAO}")
        self.resize(1020, 670)
        self.camadas_disponiveis = []
        self._init_ui()
        self.carregar_camadas_moldura()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        lbl_titulo = QLabel(f"<b>Verificar Ligação Entre Bancos v{self.VERSAO}</b>")
        fonte_tit = QFont()
        fonte_tit.setPointSize(11)
        lbl_titulo.setFont(fonte_tit)
        layout.addWidget(lbl_titulo)

        lbl_desc = QLabel(
            "1. Selecione as camadas de moldura dos bancos vizinhos (<b>mínimo 2 obrigatórias</b>).<br>"
            "2. O script identifica as conexões na divisa e verifica se no banco vizinho existe o mesmo elemento "
            "no raio de <b>1 metro</b> com atributos idênticos.<br>"
            "3. <i>Campos ignorados na comparação:</i> <code>id, observacao, operador/data de criacao/atualizacao, "
            "length_otf, area_otf e metadados '_'</code>."
        )
        lbl_desc.setStyleSheet("color: #333; margin-bottom: 5px;")
        layout.addWidget(lbl_desc)

        # Filtro de pesquisa
        h_filtro = QHBoxLayout()
        lbl_busca = QLabel("Filtrar molduras:")
        self.txt_filtro = QLineEdit()
        self.txt_filtro.setPlaceholderText("Filtrar por nome, grupo ou banco...")
        self.txt_filtro.textChanged.connect(self._filtrar_tabela)
        h_filtro.addWidget(lbl_busca)
        h_filtro.addWidget(self.txt_filtro)
        layout.addLayout(h_filtro)

        # Tabela de molduras
        self.tabela = QTableWidget(0, 8)
        self.tabela.setHorizontalHeaderLabels([
            "Selecionar", "Camada de Moldura", "Qtd Elementos", "Grupo Pai", "Banco de Dados", "Caminho no Projeto", "Geometria", "CRS"
        ])
        self.tabela.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tabela.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tabela.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tabela.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tabela.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tabela.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.tabela.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.tabela.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.tabela.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.tabela)

        # Botões de marcação e contador
        h_acoes_tabela = QHBoxLayout()
        self.btn_marcar_molduras = QPushButton("Marcar Molduras com 'moldura' no nome")
        self.btn_marcar_molduras.clicked.connect(self._marcar_todas_molduras)
        self.btn_desmarcar_todas = QPushButton("Desmarcar Todas")
        self.btn_desmarcar_todas.clicked.connect(self._desmarcar_todas)
        self.lbl_contador = QLabel("<b>0</b> camada(s) selecionada(s) (Mínimo: 2)")

        h_acoes_tabela.addWidget(self.btn_marcar_molduras)
        h_acoes_tabela.addWidget(self.btn_desmarcar_todas)
        h_acoes_tabela.addStretch()
        h_acoes_tabela.addWidget(self.lbl_contador)
        layout.addLayout(h_acoes_tabela)

        # Configurações de validação
        grp_config = QGroupBox("Parâmetros de Validação de Ligação")
        layout_config = QHBoxLayout(grp_config)

        self.chk_linhas = QCheckBox("Analisar Linhas (rodovias, drenagens, ferrovias, etc.)")
        self.chk_linhas.setChecked(True)
        self.chk_areas = QCheckBox("Analisar Áreas (massas d'água, vegetação, etc.)")
        self.chk_areas.setChecked(True)

        layout_config.addWidget(self.chk_linhas)
        layout_config.addWidget(self.chk_areas)
        layout_config.addStretch()

        lbl_tol = QLabel("Tolerância de ligação:")
        self.spin_tolerancia = QDoubleSpinBox()
        self.spin_tolerancia.setDecimals(2)
        self.spin_tolerancia.setRange(0.01, 100.0)
        self.spin_tolerancia.setSingleStep(0.5)
        self.spin_tolerancia.setValue(1.0)
        self.spin_tolerancia.setSuffix(" m")
        self.spin_tolerancia.setToolTip("Tolerância padrão de 1 metro (converte automaticamente para graus caso a camada esteja em CRS geográfico).")

        layout_config.addWidget(lbl_tol)
        layout_config.addWidget(self.spin_tolerancia)
        layout.addWidget(grp_config)

        # Barra de Progresso e Status
        self.progresso = QProgressBar()
        self.progresso.setValue(0)
        self.progresso.setVisible(False)
        layout.addWidget(self.progresso)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #0066cc; font-weight: bold;")
        layout.addWidget(self.lbl_status)

        # Botões de Execução
        h_botoes = QHBoxLayout()
        h_botoes.addStretch()
        self.btn_executar = QPushButton("Executar Validação Completa")
        self.btn_executar.setStyleSheet("background-color: #2b78e4; color: white; font-weight: bold; padding: 6px 22px;")
        self.btn_executar.clicked.connect(self.executar)
        self.btn_fechar = QPushButton("Fechar")
        self.btn_fechar.clicked.connect(self.close)

        h_botoes.addWidget(self.btn_executar)
        h_botoes.addWidget(self.btn_fechar)
        layout.addLayout(h_botoes)

    def carregar_camadas_moldura(self):
        """Lista apenas camadas de moldura não vazias."""
        self.tabela.setRowCount(0)
        self.camadas_disponiveis = []

        projeto = QgsProject.instance()
        camadas = list(projeto.mapLayers().values())

        def chave_ordem(lyr):
            nome = lyr.name().lower()
            eh_moldura = 0 if "moldura" in nome else 1
            return (eh_moldura, nome)

        camadas_ordenadas = sorted(
            [l for l in camadas if isinstance(l, QgsVectorLayer) and l.isValid()],
            key=chave_ordem
        )

        for layer in camadas_ordenadas:
            tipo_geom = layer.geometryType()
            if tipo_geom not in (QgsWkbTypes.PolygonGeometry, QgsWkbTypes.LineGeometry):
                continue

            if not camada_tem_elementos(layer):
                continue

            qtd_elementos = obter_quantidade_elementos(layer)
            row = self.tabela.rowCount()
            self.tabela.insertRow(row)

            grupo_pai, caminho = obter_info_grupos(layer)
            banco_dados = obter_nome_banco_dados(layer)
            str_geom = "Área (Polígono)" if tipo_geom == QgsWkbTypes.PolygonGeometry else "Linha"
            authid = layer.crs().authid() if layer.crs().isValid() else "S/CRS"

            chk = QCheckBox()
            chk.layer_obj = layer
            chk.stateChanged.connect(self._atualizar_contador)

            if "moldura" in layer.name().lower():
                chk.setChecked(True)

            widget_chk = QWidget()
            layout_chk = QHBoxLayout(widget_chk)
            layout_chk.addWidget(chk)
            layout_chk.setAlignment(Qt.AlignCenter)
            layout_chk.setContentsMargins(0, 0, 0, 0)
            self.tabela.setCellWidget(row, 0, widget_chk)

            # Camada
            item_camada = QTableWidgetItem(layer.name())
            item_camada.setFont(QFont("Arial", weight=QFont.Bold))
            self.tabela.setItem(row, 1, item_camada)

            # Qtd
            item_qtd = QTableWidgetItem(str(qtd_elementos))
            item_qtd.setTextAlignment(Qt.AlignCenter)
            self.tabela.setItem(row, 2, item_qtd)

            # Grupo Pai
            item_grupo = QTableWidgetItem(grupo_pai)
            if grupo_pai == "root":
                item_grupo.setForeground(QColor("#888888"))
            else:
                item_grupo.setForeground(QColor("#0055aa"))
                item_grupo.setFont(QFont("Arial", weight=QFont.Bold))
            self.tabela.setItem(row, 3, item_grupo)

            # Banco de Dados
            item_banco = QTableWidgetItem(banco_dados)
            if banco_dados != "N/A":
                item_banco.setForeground(QColor("#007722"))
            self.tabela.setItem(row, 4, item_banco)

            # Caminho
            item_caminho = QTableWidgetItem(caminho)
            item_caminho.setForeground(QColor("#555555"))
            self.tabela.setItem(row, 5, item_caminho)

            # Geometria
            self.tabela.setItem(row, 6, QTableWidgetItem(str_geom))

            # CRS
            self.tabela.setItem(row, 7, QTableWidgetItem(authid))

            self.camadas_disponiveis.append((layer, chk, row))

        self._atualizar_contador()

    def _filtrar_tabela(self, texto):
        termo = texto.strip().lower()
        for row in range(self.tabela.rowCount()):
            camada = self.tabela.item(row, 1).text().lower()
            grupo = self.tabela.item(row, 3).text().lower()
            banco = self.tabela.item(row, 4).text().lower()
            caminho = self.tabela.item(row, 5).text().lower()
            corresponde = (termo in camada) or (termo in grupo) or (termo in banco) or (termo in caminho)
            self.tabela.setRowHidden(row, not corresponde)

    def _marcar_todas_molduras(self):
        for _, chk, _ in self.camadas_disponiveis:
            if "moldura" in chk.layer_obj.name().lower():
                chk.setChecked(True)

    def _desmarcar_todas(self):
        for _, chk, _ in self.camadas_disponiveis:
            chk.setChecked(False)

    def _obter_molduras_selecionadas(self):
        selecionadas = []
        for layer, chk, _ in self.camadas_disponiveis:
            if chk.isChecked():
                selecionadas.append(layer)
        return selecionadas

    def _atualizar_contador(self):
        qtd = len(self._obter_molduras_selecionadas())
        if qtd >= 2:
            self.lbl_contador.setText(f"<b style='color: green;'>{qtd}</b> moldura(s) selecionada(s) (OK)")
            self.btn_executar.setEnabled(True)
        else:
            self.lbl_contador.setText(f"<b style='color: red;'>{qtd}</b> moldura(s) selecionada(s) (Mínimo obrigatório: 2)")
            self.btn_executar.setEnabled(False)

    def executar(self):
        molduras = self._obter_molduras_selecionadas()
        if len(molduras) < 2:
            QMessageBox.warning(
                self,
                "Atenção",
                "É obrigatório selecionar pelo menos 2 camadas de moldura para verificar a ligação entre os bancos de dados!"
            )
            return

        inc_linhas = self.chk_linhas.isChecked()
        inc_areas = self.chk_areas.isChecked()

        if not (inc_linhas or inc_areas):
            QMessageBox.warning(
                self,
                "Atenção",
                "Selecione ao menos um tipo de elemento (Linhas ou Áreas) para verificar!"
            )
            return

        tol_m = self.spin_tolerancia.value()

        self.btn_executar.setEnabled(False)
        self.progresso.setVisible(True)
        self.progresso.setValue(0)
        self.lbl_status.setText("Iniciando pipeline de validação de ligação...")
        QApplication.processEvents()

        def progresso_cb(valor, msg):
            self.progresso.setValue(valor)
            self.lbl_status.setText(msg)
            QApplication.processEvents()

        try:
            res = executar_pipeline_validacao_ligacao(
                molduras,
                incluir_linhas=inc_linhas,
                incluir_areas=inc_areas,
                tolerancia_metros=tol_m,
                callback_progresso=progresso_cb
            )

            msg = "SAPO: RELATÓRIO DE VALIDAÇÃO DE LIGAÇÃO ENTRE BANCOS\n"
            msg += "=" * 58 + "\n\n"
            msg += f"• Tolerância aplicada: {res['tolerancia_m']} metro(s)\n"
            msg += f"• Total de conexões na divisa analisadas: {res['total_analisado']}\n\n"
            msg += f"🟢 Ligações Perfeitas (OK): {res['total_ok']}\n"
            msg += f"🟡 Ligações com Atributos Divergentes: {res['total_divergentes']}\n"
            msg += f"🔴 Pontas Soltas (Sem Ligação): {res['total_sem_ligacao']}\n\n"

            if res['exemplos_divergencias']:
                msg += "Exemplos de divergências de atributos encontradas:\n"
                for ex in res['exemplos_divergencias'][:5]:
                    msg += f"  - {ex}\n"
                if len(res['exemplos_divergencias']) > 5:
                    msg += "  - ... (veja a tabela de atributos da camada para mais detalhes)\n"

            msg += "\nAs camadas categorizadas foram criadas no grupo 'Sapo - Validação de Ligação entre Bancos (1m)'."

            QMessageBox.information(self, "SAPO - Validação Concluída", msg)
            self.lbl_status.setText("Validação concluída com sucesso!")
        except Exception as e:
            QMessageBox.critical(self, "Erro na Validação", f"Ocorreu um erro durante o processamento:\n{str(e)}")
            self.lbl_status.setText("Erro durante o processamento.")
        finally:
            self.btn_executar.setEnabled(True)
            self.progresso.setVisible(False)


# =============================================================================
# ALGORITMO QGIS PROCESSING
# =============================================================================

class SapoVerificarLigacao(QgsProcessingAlgorithm):
    """
    Algoritmo QGIS Processing: SAPO - Verificar Ligação Entre Bancos
    """
    VERSAO = "1.0.0"

    PARAM_MOLDURAS = 'PARAM_MOLDURAS'
    PARAM_INCLUIR_LINHAS = 'PARAM_INCLUIR_LINHAS'
    PARAM_INCLUIR_AREAS = 'PARAM_INCLUIR_AREAS'
    PARAM_TOLERANCIA_METROS = 'PARAM_TOLERANCIA_METROS'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.PARAM_MOLDURAS,
                self.tr('Selecione as Camadas de Moldura (Mínimo 2)'),
                layerType=QgsProcessing.TypeVectorAnyGeometry
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_INCLUIR_LINHAS,
                self.tr('Verificar Linhas que tocam ou atravessam a moldura'),
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_INCLUIR_AREAS,
                self.tr('Verificar Áreas/Polígonos que tocam a moldura'),
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PARAM_TOLERANCIA_METROS,
                self.tr('Tolerância de ligação em metros'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0
            )
        )

    def name(self):
        return 'sapo_verificar_ligacao'

    def displayName(self):
        return self.tr(f'Verificar Ligação Entre Bancos v{self.VERSAO}')

    def group(self):
        return self.tr('🐸 SAPO')

    def groupId(self):
        return 'sapo'

    def shortHelpString(self):
        return self.tr(
            f"Verificar Ligação Entre Bancos v{self.VERSAO}\n\n"
            "Verifica a continuidade e integridade de ligação de linhas e áreas entre bancos vizinhos "
            "com base em suas respectivas molduras.\n\n"
            "- Filtra molduras não vazias de área ou linha.\n"
            "- Identifica conexões na divisa mútua (raio de 1 metro).\n"
            "- Compara todos os atributos (ignorando id, observacao, datas, operadores e metadados '_').\n"
            "- Gera camadas classificadas em OK, Divergentes e Sem Ligação."
        )

    def tr(self, string):
        return QCoreApplication.translate('SapoVerificarLigacao', string)

    def createInstance(self):
        return SapoVerificarLigacao()

    def createCustomParametersWidget(self, parent):
        return DialogoSapoVerificarLigacao(parent)

    def processAlgorithm(self, parameters, context, feedback):
        camadas_moldura = self.parameterAsLayerList(parameters, self.PARAM_MOLDURAS, context)
        inc_linhas = self.parameterAsBoolean(parameters, self.PARAM_INCLUIR_LINHAS, context)
        inc_areas = self.parameterAsBoolean(parameters, self.PARAM_INCLUIR_AREAS, context)
        tol_m = self.parameterAsDouble(parameters, self.PARAM_TOLERANCIA_METROS, context)

        if not camadas_moldura or len(camadas_moldura) < 2:
            raise QgsProcessingException(
                self.tr("É obrigatório selecionar pelo menos 2 camadas de moldura para verificar a ligação entre bancos de dados.")
            )

        def cb(val, msg):
            feedback.setProgress(val)
            feedback.pushInfo(msg)

        resumo = executar_pipeline_validacao_ligacao(
            camadas_moldura,
            incluir_linhas=inc_linhas,
            incluir_areas=inc_areas,
            tolerancia_metros=tol_m,
            callback_progresso=cb
        )
        return {'STATUS': 'SUCESSO', 'RESUMO': resumo}


# =============================================================================
# INICIALIZAÇÃO QUANDO EXECUTADO VIA CONSOLE / EDITOR DO QGIS
# =============================================================================
if __name__ in ('__main__', '__console__') or '__file__' not in globals():
    try:
        dialogo = DialogoSapoVerificarLigacao(iface.mainWindow() if iface else None)
        dialogo.show()
    except Exception as _err:
        print(f"Erro ao abrir janela: {_err}")
