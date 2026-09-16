# -*- coding: utf-8 -*-
"""
Script QGIS Processing: SAPO - Gerar Polígonos
Versão: 1.0.0
Grupo: SAPO
Compatibilidade: QGIS 3.24+

Pipeline unificado e 100% autossuficiente (sem dependência de scripts externos):
1. Passo 1: Gerar delimitadores auxiliares (aux_delimitadores_l e aux_delimitadores_area_edif_massa_dagua_l).
2. Passo 2: Gerar polígonos de edificação e massa d'água (massa_dagua_area, area_edificada_area).
   - Se houver flags de conflito impeditivo, interrompe o processo e limpa delimitadores (se configurado).
3. Passo 3: Gerar delimitadores de cobertura terrestre (aux_delimitadores_cobertura_l).
4. Passo 4: Gerar e classificar polígonos de cobertura terrestre com relatório final de qualidade.

Todas as camadas geradas são organizadas no grupo 'Sapo_poligonos'.
Exclusão de delimitadores temporários controlada por checkbox (marcada por padrão).
"""

import math
import traceback
from PyQt5.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingMultiStepFeedback,
    QgsProcessingParameterBoolean,
    QgsProcessingUtils,
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsSpatialIndex,
    QgsCoordinateTransform,
    QgsLayerTreeLayer,
    QgsLayerTreeNode
)
import processing


class SapoGerarPoligonos(QgsProcessingAlgorithm):

    VERSAO = "1.0.0"


    PARAM_PASSO_1 = "PARAM_PASSO_1"
    PARAM_PASSO_2 = "PARAM_PASSO_2"
    PARAM_PASSO_3 = "PARAM_PASSO_3"
    PARAM_PASSO_4 = "PARAM_PASSO_4"
    PARAM_DELETAR_DELIMITADORES = "PARAM_DELETAR_DELIMITADORES"

    NOME_GRUPO_CAMADAS = "Sapo_poligonos"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_PASSO_1,
                "1. Executar: Gerar Delimitadores Auxiliares",
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_PASSO_2,
                "2. Executar: Gerar Polígonos Edificação e Massa D'Água",
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_PASSO_3,
                "3. Executar: Gerar Delimitadores de Cobertura Terrestre",
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_PASSO_4,
                "4. Executar: Gerar e Classificar Polígonos de Cobertura Terrestre",
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_DELETAR_DELIMITADORES,
                "Remover camadas temporárias de delimitadores (aux_delimitadores_*)",
                defaultValue=True
            )
        )

    def flags(self):
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    # =========================================================================
    # GERENCIAMENTO DE CAMADAS E GRUPO
    # =========================================================================

    def _obter_ou_criar_grupo(self):
        root = QgsProject.instance().layerTreeRoot()
        grupo = root.findGroup(self.NOME_GRUPO_CAMADAS)
        if not grupo:
            grupo = root.insertGroup(0, self.NOME_GRUPO_CAMADAS)
        return grupo

    def _ativar_contagem_feicoes(self, node):
        if not node:
            return
        if isinstance(node, QgsLayerTreeLayer) or (hasattr(node, "nodeType") and node.nodeType() == QgsLayerTreeNode.NodeLayer):
            node.setCustomProperty("showFeatureCount", True)
        elif hasattr(node, "children"):
            for child in node.children():
                self._ativar_contagem_feicoes(child)

    def _organizar_novas_camadas(self, ids_anteriores):
        projeto = QgsProject.instance()
        root = projeto.layerTreeRoot()
        grupo = self._obter_ou_criar_grupo()

        novos_ids = set(projeto.mapLayers().keys()) - ids_anteriores
        for lid in novos_ids:
            node = root.findLayer(lid)
            if node:
                node.setCustomProperty("showFeatureCount", True)
                if node.parent() != grupo:
                    parent = node.parent()
                    cloned = node.clone()
                    cloned.setCustomProperty("showFeatureCount", True)
                    grupo.addChildNode(cloned)
                    parent.removeChildNode(node)

        # Garantir contagem de feições ativada para todas as camadas dentro do grupo
        self._ativar_contagem_feicoes(grupo)

    def _remover_delimitadores_temporarios(self, feedback=None):
        projeto = QgsProject.instance()
        nomes_alvo = [
            "aux_delimitadores_l",
            "aux_delimitadores_area_edif_massa_dagua_l",
            "aux_delimitadores_cobertura_l"
        ]
        removidas = []
        for nome in nomes_alvo:
            for c in projeto.mapLayersByName(nome):
                removidas.append(c.name())
                projeto.removeMapLayer(c.id())
        if removidas and feedback:
            feedback.pushInfo(f"[Limpeza] Camadas temporárias removidas: {', '.join(set(removidas))}")

    # =========================================================================
    # UTILITÁRIOS GEOMÉTRICOS COMPARTILHADOS
    # =========================================================================

    @staticmethod
    def _extrair_linhas_elementares(geom):
        if geom is None or geom.isEmpty():
            return []
        tipo = QgsWkbTypes.geometryType(geom.wkbType())
        if tipo == QgsWkbTypes.LineGeometry:
            resultado = []
            if QgsWkbTypes.isMultiType(geom.wkbType()):
                for parte in geom.asMultiPolyline():
                    if len(parte) >= 2:
                        resultado.append(QgsGeometry.fromPolylineXY(parte))
            else:
                parte = geom.asPolyline()
                if len(parte) >= 2:
                    resultado.append(QgsGeometry.fromPolylineXY(parte))
            return resultado
        elif tipo == QgsWkbTypes.PolygonGeometry:
            resultado = []
            if not QgsWkbTypes.isMultiType(geom.wkbType()):
                for anel in geom.asPolygon():
                    if len(anel) >= 2:
                        resultado.append(QgsGeometry.fromPolylineXY(anel))
            else:
                for poligono in geom.asMultiPolygon():
                    for anel in poligono:
                        if len(anel) >= 2:
                            resultado.append(QgsGeometry.fromPolylineXY(anel))
            return resultado
        return []

    @staticmethod
    def _fechar_linhas_abertas(lista_linhas, tol_dist, feedback=None):
        linhas_fechadas = []
        fechamentos = 0
        for l in lista_linhas:
            if l is None or l.isEmpty():
                continue
            partes = l.asMultiPolyline() if l.isMultipart() else [l.asPolyline()]
            for p in partes:
                if len(p) >= 3:
                    p_ini = p[0]
                    p_fim = p[-1]
                    if p_ini != p_fim:
                        dist = math.hypot(p_ini.x() - p_fim.x(), p_ini.y() - p_fim.y())
                        if dist <= tol_dist:
                            p_nova = list(p) + [p_ini]
                            linhas_fechadas.append(QgsGeometry.fromPolylineXY(p_nova))
                            fechamentos += 1
                            continue
                linhas_fechadas.append(QgsGeometry.fromPolylineXY(p) if len(p) >= 2 else l)
        if fechamentos > 0 and feedback:
            feedback.pushInfo(f"  -> {fechamentos} linha(s) com pontas abertas fechadas automaticamente.")
        return linhas_fechadas

    @staticmethod
    def _calcular_area(geom, crs_obj):
        area = geom.area()
        if crs_obj.isGeographic():
            centroide = geom.centroid()
            if centroide is not None and not centroide.isEmpty():
                lat = centroide.asPoint().y()
                area_m2 = area * (111320.0 * math.cos(math.radians(lat))) ** 2
                return area_m2
        return area

    @staticmethod
    def _atribuir_geometria_multi(feat, geom):
        if geom is None or geom.isEmpty():
            return
        if not geom.isMultipart():
            g_multi = QgsGeometry(geom)
            g_multi.convertToMultiType()
            feat.setGeometry(g_multi)
        else:
            feat.setGeometry(geom)

    @staticmethod
    def _extrair_partes_poligonais(geom):
        if geom is None or geom.isEmpty():
            return []
        resultado = []
        if geom.isMultipart():
            for parte in geom.asGeometryCollection():
                if parte is not None and not parte.isEmpty():
                    resultado.append(parte)
        else:
            resultado.append(geom)
        return resultado

    @staticmethod
    def _obter_camada_saida(saida, context=None):
        if isinstance(saida, QgsVectorLayer):
            return saida
        if isinstance(saida, str):
            if context is not None:
                try:
                    lyr = QgsProcessingUtils.mapLayerFromString(saida, context)
                    if lyr:
                        return lyr
                except Exception:
                    pass
                if hasattr(context, 'getMapLayer'):
                    try:
                        lyr = context.getMapLayer(saida)
                        if lyr:
                            return lyr
                    except Exception:
                        pass
            try:
                lyr = QgsProject.instance().mapLayer(saida)
                if lyr:
                    return lyr
            except Exception:
                pass
        return None

    # =========================================================================
    # PASSO 1: GERAR DELIMITADORES AUXILIARES
    # =========================================================================

    def _executar_passo_1(self, feedback):
        feedback.pushInfo("Iniciando geração de delimitadores auxiliares...")
        projeto = QgsProject.instance()

        NOME_SAIDA = "aux_delimitadores_l"
        NOME_SAIDA_EDIF_MASSA = "aux_delimitadores_area_edif_massa_dagua_l"
        NOME_BARRAGEM_A = "infra_barragem_a"
        NOME_BARRAGEM_L = "infra_barragem_l"
        NOME_MOLDURA = "aux_moldura_a"

        for nome_rem in [NOME_SAIDA, NOME_SAIDA_EDIF_MASSA]:
            for c_antiga in projeto.mapLayersByName(nome_rem):
                projeto.removeMapLayer(c_antiga.id())

        todas = list(projeto.mapLayers().values())
        camadas_origem = []

        for camada in todas:
            if not isinstance(camada, QgsVectorLayer):
                continue
            nome = camada.name().lower()
            if nome in [NOME_SAIDA.lower(), NOME_SAIDA_EDIF_MASSA.lower()]:
                continue
            if (nome.startswith("delimitador") or "via_deslocamento" in nome or
                "infra_barragem" in nome or "infra_ferrovia" in nome or
                "trecho_drenagem" in nome or "drenagem" in nome or
                nome == NOME_MOLDURA.lower()):
                if camada not in camadas_origem:
                    camadas_origem.append(camada)

        if not camadas_origem:
            raise Exception("Nenhuma camada de origem encontrada para os delimitadores.")

        feedback.pushInfo(f"Camadas encontradas: {[c.name() for c in camadas_origem]}")
        crs = camadas_origem[0].crs()

        # Delimitador massa d'água para toque com barragem
        linhas_delimitador_massa = []
        for c in todas:
            if not isinstance(c, QgsVectorLayer):
                continue
            n = c.name().lower()
            if "delimitador_massa" in n or "delimitador_massa_dagua" in n:
                transf_m = QgsCoordinateTransform(c.crs(), crs, projeto) if c.crs() != crs else None
                for f in c.getFeatures():
                    g = f.geometry()
                    if g and not g.isEmpty():
                        if transf_m:
                            g = QgsGeometry(g)
                            g.transform(transf_m)
                        linhas_delimitador_massa.append(g)

        geom_uniao_delimitador_massa = None
        if linhas_delimitador_massa:
            try:
                geom_uniao_delimitador_massa = QgsGeometry.unaryUnion(linhas_delimitador_massa)
            except Exception as e_union:
                feedback.pushInfo(f"[Aviso] Falha no unaryUnion das massas d'água: {e_union}")

        lat_ref = 0.0
        try:
            if crs.isGeographic():
                ext = camadas_origem[0].extent()
                lat_ref = (ext.yMinimum() + ext.yMaximum()) / 2.0
        except Exception:
            pass

        if crs.isGeographic():
            cos_lat = math.cos(math.radians(lat_ref))
            if abs(cos_lat) < 1e-5:
                cos_lat = 1.0
            tolerancia_toque = 2.0 / (111320.0 * cos_lat)
        else:
            tolerancia_toque = 2.0

        # Segmentos de barragem tocando massa
        def obter_segmentos_barragem_tocando_massa(geom, geom_massa, tol):
            if geom is None or geom.isEmpty():
                return []
            if geom_massa is None or geom_massa.isEmpty():
                return self._extrair_linhas_elementares(geom)
            aneis = []
            tipo = QgsWkbTypes.geometryType(geom.wkbType())
            if tipo == QgsWkbTypes.PolygonGeometry:
                partes = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
                for poly in partes:
                    for anel in poly:
                        aneis.append(anel)
            else:
                return self._extrair_linhas_elementares(geom)

            todos_seg = []
            for anel in aneis:
                for i in range(len(anel) - 1):
                    p1, p2 = anel[i], anel[i + 1]
                    if p1 == p2:
                        continue
                    todos_seg.append((QgsGeometry.fromPolylineXY([p1, p2]), p1, p2))
            if not todos_seg:
                return []

            seg_dois = []
            for sg, p1, p2 in todos_seg:
                if (QgsGeometry.fromPointXY(p1).distance(geom_massa) <= tol and
                    QgsGeometry.fromPointXY(p2).distance(geom_massa) <= tol):
                    seg_dois.append(sg)
            if seg_dois:
                return seg_dois

            seg_inter = []
            for sg, p1, p2 in todos_seg:
                try:
                    inter = sg.intersection(geom_massa)
                    if inter and not inter.isEmpty() and inter.length() > tol:
                        seg_inter.append(sg)
                except Exception:
                    pass
            if seg_inter:
                return seg_inter

            seg_um = []
            for sg, p1, p2 in todos_seg:
                d1 = QgsGeometry.fromPointXY(p1).distance(geom_massa)
                d2 = QgsGeometry.fromPointXY(p2).distance(geom_massa)
                if d1 <= tol or d2 <= tol:
                    p_meio = sg.interpolate(sg.length() / 2.0)
                    dm = p_meio.distance(geom_massa) if p_meio else min(d1, d2)
                    seg_um.append((dm, sg))
            if seg_um:
                seg_um.sort(key=lambda item: item[0])
                menor_d = seg_um[0][0]
                return [item[1] for item in seg_um if item[0] <= menor_d + tol]

            return self._extrair_linhas_elementares(geom)

        # Construir polígono virtual massa + barragem
        poligono_virtual_massa = None
        camadas_mb = [c for c in todas if isinstance(c, QgsVectorLayer) and
                      any(k in c.name().lower() for k in ["delimitador_massa", "infra_barragem"])]
        if camadas_mb:
            linhas_mb = []
            for c in camadas_mb:
                transf = QgsCoordinateTransform(c.crs(), crs, projeto) if c.crs() != crs else None
                eh_poly = ("infra_barragem" in c.name().lower() and c.geometryType() == QgsWkbTypes.PolygonGeometry)
                for feat in c.getFeatures():
                    g = feat.geometry()
                    if not g or g.isEmpty():
                        continue
                    if transf:
                        g = QgsGeometry(g)
                        g.transform(transf)
                    sub_l = obter_segmentos_barragem_tocando_massa(g, geom_uniao_delimitador_massa, tolerancia_toque) if eh_poly else self._extrair_linhas_elementares(g)
                    linhas_mb.extend(sub_l)

            if linhas_mb:
                try:
                    gp = QgsGeometry.polygonize(linhas_mb)
                    if gp and not gp.isEmpty():
                        poligono_virtual_massa = gp if gp.isGeosValid() else gp.makeValid()
                except Exception:
                    pass

        # Camadas de saída
        saida = QgsVectorLayer("LineString?crs=" + crs.authid(), NOME_SAIDA, "memory")
        saida_edif_massa = QgsVectorLayer("LineString?crs=" + crs.authid(), NOME_SAIDA_EDIF_MASSA, "memory")
        pr_saida = saida.dataProvider()
        pr_edif = saida_edif_massa.dataProvider()

        nomes_campos = set()
        for camada in camadas_origem:
            for campo in camada.fields():
                if campo.name() not in nomes_campos:
                    attr = [QgsField(campo.name(), campo.type(), campo.typeName(), campo.length(), campo.precision())]
                    pr_saida.addAttributes(attr)
                    pr_edif.addAttributes(attr)
                    nomes_campos.add(campo.name())

        if "origem" not in nomes_campos:
            attr_origem = [QgsField("origem", 10, "text", 100, 0)]
            pr_saida.addAttributes(attr_origem)
            pr_edif.addAttributes(attr_origem)

        saida.updateFields()
        saida_edif_massa.updateFields()

        origens_edif_massa_filtro = [
            "infra_barragem_l", "infra_barragem_a", "aux_moldura_a",
            "delimitador_massa_dagua_l", "delimitador_area_edificada_l"
        ]

        novas_feicoes = []
        novas_edif = []

        for camada in camadas_origem:
            nome_c = camada.name().lower()
            eh_drenagem = ("trecho_drenagem" in nome_c or "drenagem" in nome_c)
            eh_barr_a = ("infra_barragem" in nome_c and camada.geometryType() == QgsWkbTypes.PolygonGeometry)
            campos_orig = camada.fields()
            idx_tipo = campos_orig.indexOf("tipo")
            transf = QgsCoordinateTransform(camada.crs(), crs, projeto) if camada.crs() != crs else None

            for feat in camada.getFeatures():
                if "via_deslocamento" in nome_c and idx_tipo != -1:
                    val_t = feat[idx_tipo]
                    if str(val_t).strip() == "5":
                        continue

                g = feat.geometry()
                if not g or g.isEmpty():
                    continue
                if transf:
                    g = QgsGeometry(g)
                    g.transform(transf)

                if eh_drenagem and poligono_virtual_massa and not poligono_virtual_massa.isEmpty():
                    if poligono_virtual_massa.contains(g):
                        continue
                    if g.intersects(poligono_virtual_massa):
                        try:
                            gc = g.difference(poligono_virtual_massa)
                            if not gc or gc.isEmpty():
                                continue
                            g = gc
                        except Exception:
                            pass

                sub_l = obter_segmentos_barragem_tocando_massa(g, geom_uniao_delimitador_massa, tolerancia_toque) if eh_barr_a else self._extrair_linhas_elementares(g)

                for linha in sub_l:
                    f_nova = QgsFeature(saida.fields())
                    f_nova.setGeometry(linha)
                    valores = []
                    for fld in saida.fields():
                        if fld.name() == "origem":
                            valores.append(camada.name())
                        else:
                            idx = campos_orig.indexOf(fld.name())
                            valores.append(feat[idx] if idx >= 0 else None)
                    f_nova.setAttributes(valores)
                    novas_feicoes.append(f_nova)

                    if any(alvo in nome_c for alvo in origens_edif_massa_filtro):
                        f_edif = QgsFeature(saida_edif_massa.fields())
                        f_edif.setGeometry(linha)
                        f_edif.setAttributes(valores)
                        novas_edif.append(f_edif)

        if novas_feicoes:
            pr_saida.addFeatures(novas_feicoes)
            saida.updateExtents()
            projeto.addMapLayer(saida)

        if novas_edif:
            pr_edif.addFeatures(novas_edif)
            saida_edif_massa.updateExtents()
            projeto.addMapLayer(saida_edif_massa)

        feedback.pushInfo(f"[Passo 1 Concluído] {saida.name()}: {len(novas_feicoes)} feições | {saida_edif_massa.name()}: {len(novas_edif)} feições.")

    # =========================================================================
    # PASSO 2: GERAR POLÍGONOS DE EDIFICAÇÃO E MASSA D'ÁGUA
    # =========================================================================

    def _executar_passo_2(self, feedback, context):
        feedback.pushInfo("Iniciando geração de polígonos de edificação e massa d'água...")
        projeto = QgsProject.instance()

        NOME_CAMADA_ENTRADA = "aux_delimitadores_area_edif_massa_dagua_l"
        NOME_SAIDA_MASSA = "massa_dagua_area"
        NOME_SAIDA_EDIF = "area_edificada_area"
        NOME_FLAGS_PONTOS = "flags_erro_conflitos_p"
        NOME_FLAGS_POLIGONOS = "flags_erro_conflitos_a"

        for nome_rem in [NOME_SAIDA_MASSA, NOME_SAIDA_EDIF, NOME_FLAGS_PONTOS, NOME_FLAGS_POLIGONOS]:
            for c_antiga in projeto.mapLayersByName(nome_rem):
                projeto.removeMapLayer(c_antiga.id())

        camadas_entrada = projeto.mapLayersByName(NOME_CAMADA_ENTRADA)
        if not camadas_entrada:
            raise Exception(f"A camada de entrada '{NOME_CAMADA_ENTRADA}' não foi encontrada.")

        camada_entrada = camadas_entrada[0]
        crs = camada_entrada.crs()

        # Centróides
        camadas_centroides_todas = [
            c for c in projeto.mapLayers().values()
            if isinstance(c, QgsVectorLayer) and c.geometryType() == QgsWkbTypes.PointGeometry and "centroide" in c.name().lower()
        ]

        def localizar_camada_pontos(termos):
            for c in camadas_centroides_todas:
                nome = c.name().lower()
                if any(t in nome for t in termos):
                    return c
            return None

        camada_centroide_massa = localizar_camada_pontos(["centroide_massa_dagua", "centroide_massa"])
        camada_centroide_edif = localizar_camada_pontos(["centroide_area_edificada", "centroide_area_construida", "centroide_edificada", "centroide_edif"])

        permitidos_massa = ["ilha", "elemento_hidrografico"]
        permitidos_edif = ["massa_dagua", "massa", "ilha", "elemento_hidrografico"]

        outras_massa = [c for c in camadas_centroides_todas if c != camada_centroide_massa and not any(p in c.name().lower() for p in permitidos_massa)]
        outras_edif = [c for c in camadas_centroides_todas if c != camada_centroide_edif and not any(p in c.name().lower() for p in permitidos_edif)]

        # Tolerância
        lat_ref = 0.0
        try:
            if crs.isGeographic():
                ext = camada_entrada.extent()
                lat_ref = (ext.yMinimum() + ext.yMaximum()) / 2.0
        except Exception:
            pass

        if crs.isGeographic():
            cos_lat = math.cos(math.radians(lat_ref))
            if abs(cos_lat) < 1e-5:
                cos_lat = 1.0
            tol_fechamento_massa = 25.0 / (111320.0 * cos_lat)
        else:
            tol_fechamento_massa = 25.0

        # Flags layers
        camada_flags_pts = QgsVectorLayer("Point?crs=" + crs.authid(), NOME_FLAGS_PONTOS, "memory")
        pr_flags_pts = camada_flags_pts.dataProvider()
        pr_flags_pts.addAttributes([
            QgsField("id", QVariant.Int),
            QgsField("flag_area_id", QVariant.Int),
            QgsField("tipo_alvo", QVariant.String, len=50),
            QgsField("camada_conflito", QVariant.String, len=100),
            QgsField("motivo", QVariant.String, len=254),
            QgsField("nome_ponto", QVariant.String, len=100),
            QgsField("area_poligono_m2", QVariant.Double, prec=3)
        ])
        camada_flags_pts.updateFields()

        camada_flags_poly = QgsVectorLayer("MultiPolygon?crs=" + crs.authid(), NOME_FLAGS_POLIGONOS, "memory")
        pr_flags_poly = camada_flags_poly.dataProvider()
        pr_flags_poly.addAttributes([
            QgsField("id", QVariant.Int),
            QgsField("tipo_alvo", QVariant.String, len=50),
            QgsField("camadas_conflito", QVariant.String, len=100),
            QgsField("qtd_conflitos", QVariant.Int),
            QgsField("motivo", QVariant.String, len=254),
            QgsField("area_m2", QVariant.Double, prec=3)
        ])
        camada_flags_poly.updateFields()

        todas_flags_pts = []
        todas_flags_poly = []

        # Separar linhas
        idx_origem = camada_entrada.fields().indexOf("origem")
        linhas_massa, linhas_edif = [], []

        for feat in camada_entrada.getFeatures():
            g = feat.geometry()
            if not g or g.isEmpty():
                continue
            orig = str(feat[idx_origem] or "").lower() if idx_origem != -1 else ""
            sub_l = self._extrair_linhas_elementares(g)
            if any(k in orig for k in ["delimitador_massa", "infra_barragem", "barragem", "moldura"]):
                linhas_massa.extend(sub_l)
            linhas_edif.extend(sub_l)

        linhas_massa = self._fechar_linhas_abertas(linhas_massa, tol_fechamento_massa, feedback)
        linhas_edif = self._fechar_linhas_abertas(linhas_edif, tol_fechamento_massa, feedback)

        # Índices de centróides
        def preparar_indice(cam):
            if not cam:
                return QgsSpatialIndex(), {}
            transf = QgsCoordinateTransform(cam.crs(), crs, projeto) if cam.crs() != crs else None
            idx = QgsSpatialIndex()
            info = {}
            for f in cam.getFeatures():
                g = f.geometry()
                if not g or g.isEmpty():
                    continue
                if transf:
                    g = QgsGeometry(g)
                    g.transform(transf)
                idx.addFeature(f)
                info[f.id()] = {"geometry": g, "attrs": dict(zip(f.fields().names(), f.attributes()))}
            return idx, info

        def preparar_indice_outros(camadas):
            idx = QgsSpatialIndex()
            pts = []
            cnt = 1
            for c in camadas:
                transf = QgsCoordinateTransform(c.crs(), crs, projeto) if c.crs() != crs else None
                for f in c.getFeatures():
                    g = f.geometry()
                    if not g or g.isEmpty():
                        continue
                    if transf:
                        g = QgsGeometry(g)
                        g.transform(transf)
                    ft = QgsFeature(cnt)
                    ft.setGeometry(g)
                    idx.addFeature(ft)
                    pts.append({"camada": c.name(), "geometry": g})
                    cnt += 1
            return idx, pts

        def poligonizar_e_filtrar(linhas, nome_saida, camada_cent, outras_cent, tipo_desc, subtrair_geom=None):
            idx_cent, info_cent = preparar_indice(camada_cent)
            idx_outros, lista_outros = preparar_indice_outros(outras_cent)

            # 1. Nodar as linhas via unaryUnion
            try:
                uniao = QgsGeometry.unaryUnion(linhas)
                if uniao is not None and not uniao.isEmpty():
                    linhas_nodadas = self._extrair_linhas_elementares(uniao)
                    if linhas_nodadas:
                        linhas = linhas_nodadas
            except Exception:
                pass

            # 2. Poligonizar via QgsGeometry.polygonize (C++ GEOS rápido)
            poligonos_brutos = []
            try:
                geom_p = QgsGeometry.polygonize(linhas)
                if geom_p is not None and not geom_p.isEmpty():
                    poligonos_brutos = self._extrair_partes_poligonais(geom_p)
            except Exception:
                pass

            # 3. Fallback com native:polygonize se necessário
            if not poligonos_brutos:
                try:
                    layer_tmp = QgsVectorLayer("LineString?crs=" + crs.authid(), "temp_lines", "memory")
                    pr = layer_tmp.dataProvider()
                    feats_l = []
                    for l in linhas:
                        fl = QgsFeature()
                        fl.setGeometry(l)
                        feats_l.append(fl)
                    pr.addFeatures(feats_l)

                    res = processing.run("native:polygonize", {'INPUT': layer_tmp, 'OUTPUT': 'memory:'}, context=context, feedback=feedback, is_child_algorithm=True)
                    poly_layer = self._obter_camada_saida(res.get('OUTPUT'), context)
                    if poly_layer and poly_layer.isValid():
                        poligonos_brutos = [f.geometry() for f in poly_layer.getFeatures() if f.geometry() and not f.geometry().isEmpty()]
                except Exception as e_p:
                    feedback.pushWarning(f"Falha no fallback de native:polygonize: {e_p}")

            camada_result = QgsVectorLayer("MultiPolygon?crs=" + crs.authid(), nome_saida, "memory")
            pr_res = camada_result.dataProvider()
            campos_saida = [QgsField("id", QVariant.Int), QgsField("area_m2", QVariant.Double, prec=3)]
            if camada_cent:
                for fld in camada_cent.fields():
                    if fld.name().lower() not in ["id", "area_m2"]:
                        campos_saida.append(fld)
            pr_res.addAttributes(campos_saida)
            camada_result.updateFields()

            feicoes_salvar = []
            feicoes_geoms = []
            flag_counter = len(todas_flags_poly) + 1

            for geom_p in poligonos_brutos:
                area_m2 = self._calcular_area(geom_p, crs)
                cands_cent = idx_cent.intersects(geom_p.boundingBox())
                cent_dentro = [info_cent[cid] for cid in cands_cent if geom_p.contains(info_cent[cid]["geometry"])]

                if not cent_dentro:
                    continue

                cands_outros = idx_outros.intersects(geom_p.boundingBox())
                outros_dentro = [lista_outros[oid - 1] for oid in cands_outros if geom_p.contains(lista_outros[oid - 1]["geometry"])]

                if outros_dentro:
                    f_poly = QgsFeature(camada_flags_poly.fields())
                    self._atribuir_geometria_multi(f_poly, geom_p)
                    camadas_c = ", ".join(sorted(list(set(o["camada"] for o in outros_dentro))))
                    f_poly.setAttributes([flag_counter, tipo_desc, camadas_c, len(outros_dentro), "Conflito com centróide alheio", area_m2])
                    todas_flags_poly.append(f_poly)

                    for o in outros_dentro:
                        f_pt = QgsFeature(camada_flags_pts.fields())
                        f_pt.setGeometry(o["geometry"])
                        f_pt.setAttributes([len(todas_flags_pts) + 1, flag_counter, tipo_desc, o["camada"], "Centróide alheio dentro da área", None, area_m2])
                        todas_flags_pts.append(f_pt)

                    flag_counter += 1
                    continue

                geom_final = geom_p
                if subtrair_geom and not subtrair_geom.isEmpty():
                    try:
                        g_sub = geom_final.difference(subtrair_geom)
                        if g_sub and not g_sub.isEmpty():
                            geom_final = g_sub
                    except Exception:
                        pass

                f_out = QgsFeature(camada_result.fields())
                self._atribuir_geometria_multi(f_out, geom_final)
                attrs_val = [len(feicoes_salvar) + 1, area_m2]
                if camada_cent:
                    c_attrs = cent_dentro[0]["attrs"]
                    for fld in campos_saida[2:]:
                        attrs_val.append(c_attrs.get(fld.name(), None))
                f_out.setAttributes(attrs_val)
                feicoes_salvar.append(f_out)
                feicoes_geoms.append(geom_final)

            if feicoes_salvar:
                pr_res.addFeatures(feicoes_salvar)
                camada_result.updateExtents()
                projeto.addMapLayer(camada_result)

            geom_uniao = None
            if feicoes_geoms:
                try:
                    geom_uniao = QgsGeometry.unaryUnion(feicoes_geoms)
                except Exception:
                    pass

            feedback.pushInfo(f"  -> {nome_saida}: {len(feicoes_salvar)} polígonos mantidos.")
            return geom_uniao

        geom_uniao_massa = poligonizar_e_filtrar(linhas_massa, NOME_SAIDA_MASSA, camada_centroide_massa, outras_massa, "massa_dagua")
        poligonizar_e_filtrar(linhas_edif, NOME_SAIDA_EDIF, camada_centroide_edif, outras_edif, "area_edificada", subtrair_geom=geom_uniao_massa)

        if todas_flags_poly:
            pr_flags_poly.addFeatures(todas_flags_poly)
            camada_flags_poly.updateExtents()
            projeto.addMapLayer(camada_flags_poly)

        if todas_flags_pts:
            pr_flags_pts.addFeatures(todas_flags_pts)
            camada_flags_pts.updateExtents()
            projeto.addMapLayer(camada_flags_pts)

        return len(todas_flags_poly), len(todas_flags_pts)

    # =========================================================================
    # PASSO 3: GERAR DELIMITADORES DE COBERTURA TERRESTRE
    # =========================================================================

    def _executar_passo_3(self, feedback):
        feedback.pushInfo("Iniciando geração de delimitadores de cobertura terrestre...")
        projeto = QgsProject.instance()

        NOME_LINHAS_ENTRADA = "aux_delimitadores_l"
        NOME_POLI_EDIF = "area_edificada_area"
        NOME_POLI_MASSA = "massa_dagua_area"
        NOME_SAIDA = "aux_delimitadores_cobertura_l"
        NOME_DELIMITADOR_HIDRO = "delimitador_elemento_hidrografico_l"

        for c_antiga in projeto.mapLayersByName(NOME_SAIDA):
            projeto.removeMapLayer(c_antiga.id())

        camadas_linhas = projeto.mapLayersByName(NOME_LINHAS_ENTRADA)
        if not camadas_linhas:
            raise Exception(f"A camada de entrada '{NOME_LINHAS_ENTRADA}' não foi encontrada.")

        camada_linhas = camadas_linhas[0]
        crs_alvo = camada_linhas.crs()

        camadas_edif = projeto.mapLayersByName(NOME_POLI_EDIF)
        camada_edif = camadas_edif[0] if camadas_edif else None
        camadas_massa = projeto.mapLayersByName(NOME_POLI_MASSA)
        camada_massa = camadas_massa[0] if camadas_massa else None

        if not camada_edif and not camada_massa:
            raise Exception(f"Nenhuma das camadas de polígonos ('{NOME_POLI_EDIF}' ou '{NOME_POLI_MASSA}') foi encontrada.")

        geometrias_poligonos = []
        contornos_edif = []
        contornos_massa = []

        def coletar_poly(cam, lista_contornos):
            if not cam:
                return
            transf = QgsCoordinateTransform(cam.crs(), crs_alvo, projeto) if cam.crs() != crs_alvo else None
            for f in cam.getFeatures():
                g = f.geometry()
                if not g or g.isEmpty():
                    continue
                if transf:
                    g = QgsGeometry(g)
                    g.transform(transf)
                geometrias_poligonos.append(g)
                lista_contornos.extend(self._extrair_linhas_elementares(g))

        coletar_poly(camada_edif, contornos_edif)
        coletar_poly(camada_massa, contornos_massa)

        mascara_recorte = None
        if geometrias_poligonos:
            try:
                mascara_recorte = QgsGeometry.unaryUnion(geometrias_poligonos)
                if mascara_recorte and not mascara_recorte.isGeosValid():
                    mascara_recorte = mascara_recorte.makeValid()
            except Exception as e:
                feedback.pushInfo(f"[Aviso] Falha no unaryUnion dos polígonos: {e}")

        camada_saida = QgsVectorLayer("LineString?crs=" + crs_alvo.authid(), NOME_SAIDA, "memory")
        pr_saida = camada_saida.dataProvider()
        pr_saida.addAttributes(camada_linhas.fields())
        camada_saida.updateFields()

        idx_id = camada_saida.fields().indexOf("id")
        idx_origem = camada_saida.fields().indexOf("origem")

        novas_feicoes = []
        contador = 0

        for feat in camada_linhas.getFeatures():
            g = feat.geometry()
            if not g or g.isEmpty():
                continue

            sub_linhas_salvar = []
            if mascara_recorte and not mascara_recorte.isEmpty():
                if mascara_recorte.contains(g):
                    continue
                elif g.intersects(mascara_recorte):
                    try:
                        g_cortada = g.difference(mascara_recorte)
                        if g_cortada and not g_cortada.isEmpty():
                            sub_linhas_salvar = self._extrair_linhas_elementares(g_cortada)
                    except Exception:
                        sub_linhas_salvar = self._extrair_linhas_elementares(g)
                else:
                    sub_linhas_salvar = self._extrair_linhas_elementares(g)
            else:
                sub_linhas_salvar = self._extrair_linhas_elementares(g)

            for sl in sub_linhas_salvar:
                contador += 1
                fn = QgsFeature(camada_saida.fields())
                fn.setGeometry(sl)
                fn.setAttributes(feat.attributes())
                if idx_id != -1:
                    fn.setAttribute(idx_id, contador)
                novas_feicoes.append(fn)

        def adicionar_contornos(contornos, nome_orig):
            nonlocal contador
            for c in contornos:
                contador += 1
                fn = QgsFeature(camada_saida.fields())
                fn.setGeometry(c)
                vals = [None] * len(camada_saida.fields())
                if idx_id != -1:
                    vals[idx_id] = contador
                if idx_origem != -1:
                    vals[idx_origem] = nome_orig
                fn.setAttributes(vals)
                novas_feicoes.append(fn)

        adicionar_contornos(contornos_edif, NOME_POLI_EDIF)
        adicionar_contornos(contornos_massa, NOME_POLI_MASSA)

        # Repor delimitador hidrográfico se existir
        for c in projeto.mapLayers().values():
            if isinstance(c, QgsVectorLayer) and NOME_DELIMITADOR_HIDRO in c.name().lower():
                transf_h = QgsCoordinateTransform(c.crs(), crs_alvo, projeto) if c.crs() != crs_alvo else None
                for fh in c.getFeatures():
                    gh = fh.geometry()
                    if not gh or gh.isEmpty():
                        continue
                    if transf_h:
                        gh = QgsGeometry(gh)
                        gh.transform(transf_h)
                    for sl in self._extrair_linhas_elementares(gh):
                        contador += 1
                        fn = QgsFeature(camada_saida.fields())
                        fn.setGeometry(sl)
                        vals = [None] * len(camada_saida.fields())
                        if idx_id != -1:
                            vals[idx_id] = contador
                        if idx_origem != -1:
                            vals[idx_origem] = NOME_DELIMITADOR_HIDRO
                        fn.setAttributes(vals)
                        novas_feicoes.append(fn)

        if novas_feicoes:
            pr_saida.addFeatures(novas_feicoes)
            camada_saida.updateExtents()
            projeto.addMapLayer(camada_saida)

        feedback.pushInfo(f"[Passo 3 Concluído] '{NOME_SAIDA}' gerada com {len(novas_feicoes)} feições.")

    # =========================================================================
    # PASSO 4: GERAR E CLASSIFICAR POLÍGONOS DE COBERTURA TERRESTRE
    # =========================================================================

    def _executar_passo_4(self, feedback, context):
        feedback.pushInfo("Iniciando poligonização e classificação da cobertura terrestre...")
        projeto = QgsProject.instance()

        NOME_CAMADA_LINHAS = "aux_delimitadores_cobertura_l"
        NOME_FLAGS_VAZIAS = "flags_areas_vazias_a"
        NOME_FLAGS_DUPLOS = "flags_centroide_duplo_p"

        camadas_linhas = projeto.mapLayersByName(NOME_CAMADA_LINHAS)
        if not camadas_linhas:
            raise Exception(f"A camada '{NOME_CAMADA_LINHAS}' não foi encontrada no projeto.")

        camada_linhas = camadas_linhas[0]
        crs_alvo = camada_linhas.crs()

        termos_ignorar = ["massa_dagua", "massa_d_agua", "area_edificada", "area_construida", "edificada", "edif"]
        camadas_centroides = []
        for c in projeto.mapLayers().values():
            if not isinstance(c, QgsVectorLayer) or c.geometryType() != QgsWkbTypes.PointGeometry:
                continue
            nc = c.name().lower().strip()
            if "flag" in nc:
                continue
            if "centroide" in nc and not any(t in nc for t in termos_ignorar):
                camadas_centroides.append(c)

        if not camadas_centroides:
            raise Exception("Nenhuma camada de centróide para cobertura terrestre encontrada.")

        def extrair_nome_saida(nome_c):
            n = nome_c.lower().strip()
            if n.startswith("centroide_"):
                n = n[len("centroide_"):]
            if n.endswith("_p"):
                n = n[:-2]
            return f"{n}_area"

        mapa_nomes = {c.name(): extrair_nome_saida(c.name()) for c in camadas_centroides}

        for nome_rem in [NOME_FLAGS_VAZIAS, NOME_FLAGS_DUPLOS] + list(set(mapa_nomes.values())):
            for c_antiga in projeto.mapLayersByName(nome_rem):
                projeto.removeMapLayer(c_antiga.id())

        lat_ref = 0.0
        try:
            if crs_alvo.isGeographic():
                ext = camada_linhas.extent()
                lat_ref = (ext.yMinimum() + ext.yMaximum()) / 2.0
        except Exception:
            pass

        if crs_alvo.isGeographic():
            cos_lat = math.cos(math.radians(lat_ref))
            if abs(cos_lat) < 1e-5:
                cos_lat = 1.0
            tol_fechamento = 25.0 / (111320.0 * cos_lat)
        else:
            tol_fechamento = 25.0

        linhas_todas = []
        for f in camada_linhas.getFeatures():
            linhas_todas.extend(self._extrair_linhas_elementares(f.geometry()))

        linhas_todas = self._fechar_linhas_abertas(linhas_todas, tol_fechamento, feedback)

        # 1. Nodar linhas com unaryUnion
        try:
            linhas_unidas = QgsGeometry.unaryUnion(linhas_todas)
            partes_linhas = self._extrair_linhas_elementares(linhas_unidas)
            if partes_linhas:
                linhas_todas = partes_linhas
        except Exception:
            pass

        # 2. Poligonizar direto via QgsGeometry.polygonize (C++ GEOS rápido)
        poligonos_brutos = []
        try:
            geom_p = QgsGeometry.polygonize(linhas_todas)
            if geom_p is not None and not geom_p.isEmpty():
                poligonos_brutos = self._extrair_partes_poligonais(geom_p)
        except Exception:
            pass

        # 3. Fallback com native:polygonize se necessário
        if not poligonos_brutos:
            try:
                layer_temp = QgsVectorLayer("LineString?crs=" + crs_alvo.authid(), "temp_lines", "memory")
                pr_temp = layer_temp.dataProvider()
                feats_t = []
                for l in linhas_todas:
                    ft = QgsFeature()
                    ft.setGeometry(l)
                    feats_t.append(ft)
                pr_temp.addFeatures(feats_t)

                res = processing.run("native:polygonize", {'INPUT': layer_temp, 'OUTPUT': 'memory:'}, context=context, feedback=feedback, is_child_algorithm=True)
                poly_layer = self._obter_camada_saida(res.get('OUTPUT'), context)
                if poly_layer and poly_layer.isValid():
                    poligonos_brutos = [f.geometry() for f in poly_layer.getFeatures() if f.geometry() and not f.geometry().isEmpty()]
            except Exception as e_p:
                feedback.pushWarning(f"Falha no fallback de native:polygonize: {e_p}")

        # Índices espaciais dos centróides
        indices_centroides = {}
        info_pontos = {}
        for c in camadas_centroides:
            idx = QgsSpatialIndex()
            info_dict = {}
            transf = QgsCoordinateTransform(c.crs(), crs_alvo, projeto) if c.crs() != crs_alvo else None
            for feat in c.getFeatures():
                g = feat.geometry()
                if not g or g.isEmpty():
                    continue
                if transf:
                    g = QgsGeometry(g)
                    g.transform(transf)
                f_tmp = QgsFeature(feat.id())
                f_tmp.setGeometry(g)
                idx.addFeature(f_tmp)
                info_dict[feat.id()] = {"geometry": g, "attrs": dict(zip(feat.fields().names(), feat.attributes()))}
            indices_centroides[c.name()] = idx
            info_pontos[c.name()] = info_dict

        # Criar camadas de saída
        camadas_saida = {}
        for c in camadas_centroides:
            nome_dest = mapa_nomes[c.name()]
            if nome_dest not in camadas_saida:
                lyr = QgsVectorLayer("MultiPolygon?crs=" + crs_alvo.authid(), nome_dest, "memory")
                pr = lyr.dataProvider()
                campos = [QgsField("id", QVariant.Int), QgsField("area_m2", QVariant.Double, prec=3)]
                for fld in c.fields():
                    if fld.name().lower() not in ["id", "area_m2"]:
                        campos.append(fld)
                pr.addAttributes(campos)
                lyr.updateFields()
                camadas_saida[nome_dest] = {"layer": lyr, "provider": pr, "feicoes": [], "campos": campos}

        # Flags
        camada_flags_vazias = QgsVectorLayer("MultiPolygon?crs=" + crs_alvo.authid(), NOME_FLAGS_VAZIAS, "memory")
        pr_flags_vazias = camada_flags_vazias.dataProvider()
        pr_flags_vazias.addAttributes([QgsField("id", QVariant.Int), QgsField("area_m2", QVariant.Double, prec=3), QgsField("motivo", QVariant.String, len=100)])
        camada_flags_vazias.updateFields()
        feicoes_flags_vazias = []

        camada_flags_duplos = QgsVectorLayer("Point?crs=" + crs_alvo.authid(), NOME_FLAGS_DUPLOS, "memory")
        pr_flags_duplos = camada_flags_duplos.dataProvider()
        pr_flags_duplos.addAttributes([
            QgsField("id", QVariant.Int),
            QgsField("area_poligono_id", QVariant.Int),
            QgsField("camada_origem", QVariant.String, len=100),
            QgsField("total_pontos_area", QVariant.Int),
            QgsField("camadas_conflitantes", QVariant.String, len=200),
            QgsField("area_m2", QVariant.Double, prec=3)
        ])
        camada_flags_duplos.updateFields()
        # Recortar polígonos coincidentes com área edificada e massa d'água
        geoms_edif_massa = []
        for nome_cam_base in ["area_edificada_area", "massa_dagua_area"]:
            for cam_base in projeto.mapLayersByName(nome_cam_base):
                transf_b = QgsCoordinateTransform(cam_base.crs(), crs_alvo, projeto) if cam_base.crs() != crs_alvo else None
                for f_b in cam_base.getFeatures():
                    g_b = f_b.geometry()
                    if not g_b or g_b.isEmpty():
                        continue
                    if transf_b is not None:
                        g_b = QgsGeometry(g_b)
                        g_b.transform(transf_b)
                    geoms_edif_massa.append(g_b)

        mascara_edif_massa = None
        if geoms_edif_massa:
            try:
                mascara_edif_massa = QgsGeometry.unaryUnion(geoms_edif_massa)
            except Exception as e_m:
                feedback.pushWarning(f"Falha na união de edif e massa: {e_m}")
                mascara_edif_massa = None

        if mascara_edif_massa and not mascara_edif_massa.isEmpty():
            poligonos_filtrados = []
            bbox_mascara = mascara_edif_massa.boundingBox()

            for p in poligonos_brutos:
                if not p or p.isEmpty():
                    continue
                bbox_p = p.boundingBox()

                eh_sobreposto_massa = False
                for c_name, idx_c in indices_centroides.items():
                    cn = c_name.lower()
                    if "elemento_hidrografico" in cn or "ilha" in cn:
                        cands = idx_c.intersects(bbox_p)
                        if any(p.contains(info_pontos[c_name][pid]["geometry"]) for pid in cands):
                            eh_sobreposto_massa = True
                            break

                if eh_sobreposto_massa or not bbox_mascara.intersects(bbox_p):
                    poligonos_filtrados.append(p)
                    continue

                if mascara_edif_massa.contains(p):
                    continue

                if p.intersects(mascara_edif_massa):
                    try:
                        p_cortado = p.difference(mascara_edif_massa)
                        if p_cortado and not p_cortado.isEmpty():
                            for pt in self._extrair_partes_poligonais(p_cortado):
                                if self._calcular_area(pt, crs_alvo) >= 1.0:
                                    poligonos_filtrados.append(pt)
                    except Exception:
                        poligonos_filtrados.append(p)
                else:
                    poligonos_filtrados.append(p)

            poligonos_brutos = poligonos_filtrados

        feicoes_flags_duplos = []
        cnt_vazias = 0
        cnt_duplos = 0

        for id_poly, geom_poly in enumerate(poligonos_brutos, 1):
            bbox = geom_poly.boundingBox()
            cent_dentro = []

            for c in camadas_centroides:
                c_name = c.name()
                cands = indices_centroides[c_name].intersects(bbox)
                for pt_id in cands:
                    pt_info = info_pontos[c_name][pt_id]
                    if geom_poly.contains(pt_info["geometry"]):
                        cent_dentro.append({"camada": c_name, "camada_saida": mapa_nomes[c_name], "geometry": pt_info["geometry"], "attrs": pt_info["attrs"]})

            area_m2 = self._calcular_area(geom_poly, crs_alvo)

            if not cent_dentro:
                cnt_vazias += 1
                fv = QgsFeature(camada_flags_vazias.fields())
                self._atribuir_geometria_multi(fv, geom_poly)
                fv.setAttributes([cnt_vazias, area_m2, "Área sem centróide de cobertura"])
                feicoes_flags_vazias.append(fv)
                continue

            if len(cent_dentro) > 1:
                camadas_conf = ", ".join(sorted(list(set(cd["camada"] for cd in cent_dentro))))
                for cd in cent_dentro:
                    cnt_duplos += 1
                    fp = QgsFeature(camada_flags_duplos.fields())
                    fp.setGeometry(cd["geometry"])
                    fp.setAttributes([cnt_duplos, id_poly, cd["camada"], len(cent_dentro), camadas_conf, area_m2])
                    feicoes_flags_duplos.append(fp)

            escolhido = cent_dentro[0]
            info_dest = camadas_saida[escolhido["camada_saida"]]
            f_poly = QgsFeature(info_dest["layer"].fields())
            self._atribuir_geometria_multi(f_poly, geom_poly)
            vals = [len(info_dest["feicoes"]) + 1, area_m2]
            for fld in info_dest["campos"][2:]:
                vals.append(escolhido["attrs"].get(fld.name(), None))
            f_poly.setAttributes(vals)
            info_dest["feicoes"].append(f_poly)

        for nome_dest, info_d in camadas_saida.items():
            if info_d["feicoes"]:
                info_d["provider"].addFeatures(info_d["feicoes"])
                info_d["layer"].updateExtents()
                projeto.addMapLayer(info_d["layer"])
                feedback.pushInfo(f"  -> {nome_dest}: {len(info_d['feicoes'])} polígono(s).")

        if feicoes_flags_vazias:
            pr_flags_vazias.addFeatures(feicoes_flags_vazias)
            camada_flags_vazias.updateExtents()
            projeto.addMapLayer(camada_flags_vazias)

        if feicoes_flags_duplos:
            pr_flags_duplos.addFeatures(feicoes_flags_duplos)
            camada_flags_duplos.updateExtents()
            projeto.addMapLayer(camada_flags_duplos)

        return len(feicoes_flags_vazias), len(feicoes_flags_duplos)

    # =========================================================================
    # PROCESS ALGORITHM (ORQUESTRADOR)
    # =========================================================================

    def processAlgorithm(self, parameters, context, model_feedback):
        rodar_p1 = self.parameterAsBool(parameters, self.PARAM_PASSO_1, context)
        rodar_p2 = self.parameterAsBool(parameters, self.PARAM_PASSO_2, context)
        rodar_p3 = self.parameterAsBool(parameters, self.PARAM_PASSO_3, context)
        rodar_p4 = self.parameterAsBool(parameters, self.PARAM_PASSO_4, context)
        deletar_delimitadores = self.parameterAsBool(parameters, self.PARAM_DELETAR_DELIMITADORES, context)

        total_passos = sum([rodar_p1, rodar_p2, rodar_p3, rodar_p4])
        if total_passos == 0:
            model_feedback.pushWarning("Nenhum passo foi selecionado para execução.")
            return {}

        feedback = QgsProcessingMultiStepFeedback(total_passos, model_feedback)
        passo_atual = 0
        projeto = QgsProject.instance()

        self._obter_ou_criar_grupo()

        feedback.pushInfo("=" * 60)
        feedback.pushInfo(f"SAPO - Gerar Polígonos (Versão {self.VERSAO})")
        feedback.pushInfo("=" * 60)

        # ============================================================
        # PASSO 1
        # ============================================================
        if rodar_p1:
            if feedback.isCanceled():
                return {}
            feedback.setCurrentStep(passo_atual)
            feedback.pushInfo("\n" + "=" * 60)
            feedback.pushInfo("ETAPA 1/4: Gerar Delimitadores Auxiliares")
            feedback.pushInfo("=" * 60)

            ids_antes = set(projeto.mapLayers().keys())
            try:
                self._executar_passo_1(feedback)
            except Exception as e:
                feedback.reportError(f"Erro no Passo 1: {e}")
                feedback.reportError(traceback.format_exc())
                return {"erro": str(e), "etapa": 1}

            self._organizar_novas_camadas(ids_antes)
            passo_atual += 1

        # ============================================================
        # PASSO 2
        # ============================================================
        if rodar_p2:
            if feedback.isCanceled():
                return {}
            feedback.setCurrentStep(passo_atual)
            feedback.pushInfo("\n" + "=" * 60)
            feedback.pushInfo("ETAPA 2/4: Gerar Polígonos Edificação e Massa D'Água")
            feedback.pushInfo("=" * 60)

            ids_antes = set(projeto.mapLayers().keys())
            try:
                qtd_poly_erro, qtd_pts_erro = self._executar_passo_2(feedback, context)
            except Exception as e:
                feedback.reportError(f"Erro no Passo 2: {e}")
                feedback.reportError(traceback.format_exc())
                return {"erro": str(e), "etapa": 2}

            self._organizar_novas_camadas(ids_antes)

            # Verificação de flags de conflito no Passo 2
            if qtd_poly_erro > 0 or qtd_pts_erro > 0:
                feedback.pushWarning("")
                feedback.pushWarning("!" * 65)
                feedback.pushWarning("  PARADA DO PROCESSO: CONFLITOS DETECTADOS NO PASSO 2")
                feedback.pushWarning("!" * 65)
                feedback.pushWarning(
                    f"Foram geradas feições de conflito impeditivo:\n"
                    f"  -> flags_erro_conflitos_a: {qtd_poly_erro} polígono(s) com conflito.\n"
                    f"  -> flags_erro_conflitos_p: {qtd_pts_erro} geometria(s) conflitante(s)."
                )

                if deletar_delimitadores:
                    self._remover_delimitadores_temporarios(feedback)

                self._ativar_contagem_feicoes(self._obter_ou_criar_grupo())

                feedback.pushWarning("")
                feedback.pushWarning(">>> Para continuar o processo:")
                feedback.pushWarning("1. Inspecione e corrija os conflitos identificados pelas camadas de flags.")
                feedback.pushWarning("2. Em seguida, execute novamente o algoritmo.")
                feedback.pushWarning("!" * 65)

                return {
                    "status": "interrompido_por_conflitos",
                    "etapa_parada": 2,
                    "flags_erro_conflitos_a": qtd_poly_erro,
                    "flags_erro_conflitos_p": qtd_pts_erro
                }

            feedback.pushInfo("[OK] Nenhum conflito impeditivo encontrado no Passo 2.")
            passo_atual += 1

        # ============================================================
        # PASSO 3
        # ============================================================
        if rodar_p3:
            if feedback.isCanceled():
                return {}
            feedback.setCurrentStep(passo_atual)
            feedback.pushInfo("\n" + "=" * 60)
            feedback.pushInfo("ETAPA 3/4: Gerar Delimitadores de Cobertura Terrestre")
            feedback.pushInfo("=" * 60)

            ids_antes = set(projeto.mapLayers().keys())
            try:
                self._executar_passo_3(feedback)
            except Exception as e:
                feedback.reportError(f"Erro no Passo 3: {e}")
                feedback.reportError(traceback.format_exc())
                return {"erro": str(e), "etapa": 3}

            self._organizar_novas_camadas(ids_antes)
            passo_atual += 1

        # ============================================================
        # PASSO 4
        # ============================================================
        if rodar_p4:
            if feedback.isCanceled():
                return {}
            feedback.setCurrentStep(passo_atual)
            feedback.pushInfo("\n" + "=" * 60)
            feedback.pushInfo("ETAPA 4/4: Gerar e Classificar Polígonos de Cobertura")
            feedback.pushInfo("=" * 60)

            ids_antes = set(projeto.mapLayers().keys())
            try:
                qtd_vazias, qtd_duplos = self._executar_passo_4(feedback, context)
            except Exception as e:
                feedback.reportError(f"Erro no Passo 4: {e}")
                feedback.reportError(traceback.format_exc())
                return {"erro": str(e), "etapa": 4}

            self._organizar_novas_camadas(ids_antes)

            feedback.pushInfo("")
            feedback.pushInfo("=" * 60)
            feedback.pushInfo("RELATÓRIO DE QUALIDADE FINAL (COBERTURA TERRESTRE)")
            feedback.pushInfo("=" * 60)

            if qtd_vazias > 0 or qtd_duplos > 0:
                feedback.pushWarning("Atenção: Foram geradas flags de verificação na etapa final:")
                if qtd_vazias > 0:
                    feedback.pushWarning(f"  -> flags_areas_vazias_a: {qtd_vazias} área(s) sem centróide.")
                if qtd_duplos > 0:
                    feedback.pushWarning(f"  -> flags_centroide_duplo_p: {qtd_duplos} ponto(s) com conflito de centróide duplo.")
                feedback.pushWarning("Inspecione as camadas de flags no QGIS para efetuar as correções necessárias.")
            else:
                feedback.pushInfo("[Sucesso] Todas as áreas de cobertura foram classificadas sem flags.")

            feedback.pushInfo("=" * 60)
            passo_atual += 1

        # ============================================================
        # LIMPEZA FINAL
        # ============================================================
        if deletar_delimitadores:
            self._remover_delimitadores_temporarios(feedback)

        # Ativar contagem de feições em todas as camadas do grupo Sapo_poligonos
        self._ativar_contagem_feicoes(self._obter_ou_criar_grupo())

        feedback.pushInfo("\nTodos os passos selecionados foram concluídos com sucesso.")
        return {"status": "sucesso"}

    def name(self):
        return "sapo_gerar_poligonos"

    def displayName(self):
        return f"Gerar Polígonos v{self.VERSAO}"

    def group(self):
        return "🐸 SAPO"

    def groupId(self):
        return "sapo"

    def createInstance(self):
        return SapoGerarPoligonos()

    def shortHelpString(self):
        return QCoreApplication.translate(
            "SapoGerarPoligonos",
            f"Versão: {self.VERSAO}\n\n"
            "Algoritmo unificado e autossuficiente para poligonização cartográfica do SAPO:\n\n"
            "1. Passo 1: Extrai e prepara todas as linhas delimitadoras (estradas, ferrovias, "
            "drenagens tratadas, barragens, moldura).\n\n"
            "2. Passo 2: Poligoniza e separa as áreas de edificação e massa d'água.\n"
            "   * ATENÇÃO: Se forem detectados conflitos impeditivos (flags_erro_conflitos_p "
            "ou flags_erro_conflitos_a), a execução será INTERROMPIDA automaticamente e os delimitadores "
            "temporários serão excluídos (se a opção estiver ativa).\n\n"
            "3. Passo 3: Recorta as linhas fora das áreas edificadas e massas d'água e adiciona "
            "seus contornos para formar a grade da cobertura.\n\n"
            "4. Passo 4: Poligoniza e classifica todas as áreas de cobertura terrestre "
            "(vegetação, terreno exposto, etc.), gerando flags de áreas vazias ou centróides duplos se existirem.\n\n"
            "Organização e Limpeza:\n"
            "- Todas as camadas geradas são organizadas no grupo 'Sapo_poligonos' no painel de camadas.\n"
            "- A opção 'Remover camadas temporárias de delimitadores' (marcada por padrão) remove automaticamente "
            "aux_delimitadores_l, aux_delimitadores_area_edif_massa_dagua_l e aux_delimitadores_cobertura_l "
            "ao término ou se o processo for interrompido por flags no Passo 2."
        )
