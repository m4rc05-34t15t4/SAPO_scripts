# -*- coding: utf-8 -*-
"""
Script QGIS Processing: Corrigir Drenagem v1.0.0
Versão: 1.0.0
Grupo: SAPO
Compatibilidade: QGIS 3.24+ / 3.28+ / 3.34+

Pipeline unificado e modular para correção de hidrografia x relevo:
1. Etapa 1/4: Detecção de Conflitos (flags_drenagem_curva com qtd_conexoes >= 2).
   - Se a camada 'flags_drenagem_curva' já existir no projeto, pula o cálculo inicial.
2. Etapa 2/4: Geração de Eixos Centrais Puros ('esqueleto_curva_bbox').
   - Conecta no início do montante, evita toque de crista e conecta suavemente a jusante.
   - Herda o id_drenagem e dados da curva.
   - Camada temporária 'esqueleto_curva_bbox' é preservada no projeto e NÃO é excluída no final.
   - Se já existir no projeto, o fluxo pula automaticamente as Etapas 1 e 2 e inicia direto na atualização.
3. Etapa 3/4: Atualização da Drenagem (configurável via checkbox).
   - Pré-valida e executa o Backup da camada original em GeoPackage (.gpkg).
   - Emenda os trechos recalculados na camada de hidrografia a partir dos esqueletos.
4. Etapa 4/4: Revalidação e Saldo Final de Conflitos (configurável via checkbox).
   - Substitui a camada de flags anterior recalculando as interseções finais com a drenagem atualizada.
   - Exibe o saldo exato de flags remanescentes para inspeção/edição manual.

Controle de Execução:
- Checkboxes independentes para cada um dos 4 processos (todos marcados por padrão),
  permitindo executar o fluxo completo ou rodar qualquer etapa de forma isolada.
"""

import os
import math
import heapq
import traceback
from collections import defaultdict

from PyQt5.QtCore import QCoreApplication, QVariant, Qt, QDir
from PyQt5.QtWidgets import QFileDialog, QMessageBox

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingMultiStepFeedback,
    QgsProcessingFeedback,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsPoint,
    QgsLineString,
    QgsWkbTypes,
    QgsFeatureRequest,
    QgsMarkerSymbol,
    QgsLineSymbol,
    QgsSingleSymbolRenderer,
    QgsDataSourceUri,
    QgsVectorFileWriter,
    QgsLayerTreeLayer,
    QgsLayerTreeNode
)
from qgis.PyQt.QtGui import QColor
import processing

import shapely.wkb
from shapely.geometry import Point, LineString, MultiLineString, Polygon
from shapely.ops import unary_union

try:
    from scipy.spatial import Voronoi
    TEM_SCIPY = True
except ImportError:
    TEM_SCIPY = False


class SubStepFeedback(QgsProcessingFeedback):
    """Encaminha o progresso contínuo de algoritmos nativos (ex: C++) para o feedback do processamento."""
    def __init__(self, parent_feedback, start_pct, end_pct, label):
        super().__init__()
        self.parent_feedback = parent_feedback
        self.start_pct = start_pct
        self.end_pct = end_pct
        self.label = label
        self.last_pct = -1

    def setProgress(self, progress):
        super().setProgress(progress)
        val = int(self.start_pct + (progress / 100.0) * (self.end_pct - self.start_pct))
        if val != self.last_pct:
            self.last_pct = val
            self.parent_feedback.setProgress(val)
            self.parent_feedback.setProgressText(f"{self.label} ({int(progress)}%)...")

    def isCanceled(self):
        return self.parent_feedback.isCanceled()

    def reportError(self, error, fatalError=False):
        self.parent_feedback.reportError(error, fatalError)

    def pushInfo(self, info):
        self.parent_feedback.pushInfo(info)

    def pushWarning(self, warning):
        self.parent_feedback.pushWarning(warning)


class Sapo_Corrigir_Drenagem(QgsProcessingAlgorithm):

    VERSAO = "1.0.0"

    PARAM_DRENAGEM = "PARAM_DRENAGEM"
    PARAM_CURVA = "PARAM_CURVA"
    PARAM_ETAPA_1 = "PARAM_ETAPA_1"
    PARAM_ETAPA_2 = "PARAM_ETAPA_2"
    PARAM_ETAPA_3 = "PARAM_ETAPA_3"
    PARAM_ETAPA_4 = "PARAM_ETAPA_4"
    PARAM_APENAS_SELECIONADAS = "PARAM_APENAS_SELECIONADAS"
    PARAM_ATUALIZAR_DRENAGEM = PARAM_ETAPA_3  # Retrocompatibilidade
    PARAM_BACKUP_GPKG = "PARAM_BACKUP_GPKG"

    NOME_CAMADA_FLAGS = "flags_drenagem_curva"
    NOME_CAMADA_ESQUELETO = "esqueleto_curva_bbox"
    NOME_GRUPO_CAMADAS = "Sapo_Drenagem_Curva"

    PASSO_DENSIFICACAO = 2.0
    TOLERANCIA_SUAVIZACAO = 0.6
    ITERACOES_CHAIKIN = 2
    COMPRIMENTO_LINHA_IMAGINARIA = 2000.0

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.PARAM_DRENAGEM,
                "Camada de Trecho de Drenagem",
                types=[QgsProcessing.TypeVectorLine],
                defaultValue="elemnat_trecho_drenagem_l"
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.PARAM_CURVA,
                "Camada de Curvas de Nível",
                types=[QgsProcessing.TypeVectorLine],
                defaultValue="elemnat_curva_nivel_l"
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_ETAPA_1,
                "1. Executar Etapa 1: Detecção de Conflitos (flags_drenagem_curva)",
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_ETAPA_2,
                "2. Executar Etapa 2: Geração de Eixos Centrais (esqueleto_curva_bbox)",
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_ETAPA_3,
                "3. Executar Etapa 3: Atualização da Camada de Drenagem",
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_ETAPA_4,
                "4. Executar Etapa 4: Revalidação e Saldo Final de Flags",
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARAM_APENAS_SELECIONADAS,
                "Processar apenas feições selecionadas (se houver seleção nas flags ou esqueletos)",
                defaultValue=False
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.PARAM_BACKUP_GPKG,
                "Local para salvar Backup da Drenagem Original (GeoPackage)",
                fileFilter="GeoPackage (*.gpkg)",
                optional=True
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

    def _organizar_camada_no_grupo(self, camada):
        if not camada:
            return
        projeto = QgsProject.instance()
        root = projeto.layerTreeRoot()
        grupo = self._obter_ou_criar_grupo()

        node = root.findLayer(camada.id())
        if node and node.parent() != grupo:
            parent = node.parent()
            cloned = node.clone()
            cloned.setCustomProperty("showFeatureCount", True)
            grupo.addChildNode(cloned)
            parent.removeChildNode(node)
        self._ativar_contagem_feicoes(grupo)

    def _obter_camada_projeto(self, nome):
        projeto = QgsProject.instance()
        camadas = projeto.mapLayersByName(nome)
        if camadas:
            return camadas[0]
        for lyr in projeto.mapLayers().values():
            if nome.lower() in lyr.name().lower():
                return lyr
        return None

    def _remover_camada_por_nome(self, nome):
        projeto = QgsProject.instance()
        velhas = projeto.mapLayersByName(nome)
        for v in velhas:
            projeto.removeMapLayer(v.id())

    # =========================================================================
    # BACKUP DA DRENAGEM
    # =========================================================================
    def _obter_nome_banco(self, camada):
        fonte = camada.source()
        uri = QgsDataSourceUri(fonte)
        if uri.database():
            return uri.database()
        caminho_arquivo = fonte.split('|')[0].strip('"').strip("'")
        if os.path.exists(caminho_arquivo):
            nome_base = os.path.basename(caminho_arquivo)
            return os.path.splitext(nome_base)[0]
        projeto = QgsProject.instance()
        if projeto.title():
            return projeto.title()
        if projeto.fileName():
            return os.path.splitext(os.path.basename(projeto.fileName()))[0]
        return "banco"

    def _executar_backup(self, lyr_drenagem, caminho_destino, feedback):
        nome_banco = self._obter_nome_banco(lyr_drenagem)
        nome_sugerido = f"{nome_banco}_{lyr_drenagem.name()}_backup.gpkg"

        if not caminho_destino:
            pasta_inicial = QgsProject.instance().homePath()
            if not pasta_inicial or not os.path.exists(pasta_inicial):
                pasta_inicial = QDir.homePath()
            caminho_sugerido = os.path.join(pasta_inicial, nome_sugerido)
            caminho_destino, _ = QFileDialog.getSaveFileName(
                None,
                "Defina o local para salvar o Backup da Drenagem Original",
                caminho_sugerido,
                "GeoPackage (*.gpkg)"
            )

        if not caminho_destino:
            return None

        if not caminho_destino.lower().endswith(".gpkg"):
            caminho_destino += ".gpkg"

        feedback.pushInfo(f"[Backup] Salvando cópia de segurança em: {caminho_destino}")

        projeto = QgsProject.instance()
        for l_id, l in list(projeto.mapLayers().items()):
            if caminho_destino.lower() in l.source().lower():
                projeto.removeMapLayer(l_id)

        if os.path.exists(caminho_destino):
            try:
                os.remove(caminho_destino)
                for ext in ["-wal", "-shm"]:
                    f_temp = caminho_destino + ext
                    if os.path.exists(f_temp):
                        try:
                            os.remove(f_temp)
                        except Exception:
                            pass
            except Exception as e:
                feedback.reportError(f"Não foi possível sobrescrever o backup existente: {e}")
                return None

        opcoes = QgsVectorFileWriter.SaveVectorOptions()
        opcoes.driverName = "GPKG"
        opcoes.layerName = lyr_drenagem.name()
        opcoes.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

        res = QgsVectorFileWriter.writeAsVectorFormatV3(
            lyr_drenagem,
            caminho_destino,
            projeto.transformContext(),
            opcoes
        )

        if res[0] != QgsVectorFileWriter.NoError:
            feedback.reportError(f"Falha ao salvar backup: {res[1]}")
            return None

        feedback.pushInfo(f"[Backup] Backup concluído com sucesso!")
        return caminho_destino

    # =========================================================================
    # ETAPA 1 E 4: DETECÇÃO DE CONFLITOS (INTERSEÇÃO DRENAGEM X CURVA)
    # =========================================================================
    def _calcular_intersecoes(self, lyr_drenagem, lyr_curva, feedback_step, context, etapa_num=1):
        campo_id_dren = next(
            (f.name() for f in lyr_drenagem.fields() if f.name().lower() in ['id', 'id_drenagem', 'fid', 'id_objeto', 'id_trecho_drenagem']),
            lyr_drenagem.fields()[0].name()
        )
        campo_id_curva = next(
            (f.name() for f in lyr_curva.fields() if f.name().lower() in ['id', 'id_curva', 'fid', 'id_objeto']),
            lyr_curva.fields()[0].name()
        )
        campo_cota = next(
            (f.name() for f in lyr_curva.fields() if f.name().lower() in ['cota', 'elevation', 'altitude', 'z', 'altimetria']),
            None
        )

        if not campo_cota:
            feedback_step.reportError("Campo de cota não encontrado na camada de curvas de nível!")
            return None

        feedback_step.pushInfo(f"[Etapa {etapa_num}/4] Iniciando cruzamento geométrico nativo C++ (native:lineintersections)...")
        sub_fb = SubStepFeedback(feedback_step, 0, 70, f"Etapa {etapa_num}/4: Calculando interseções C++")

        params = {
            'INPUT': lyr_drenagem,
            'INTERSECT': lyr_curva,
            'INPUT_FIELDS': [campo_id_dren],
            'INTERSECT_FIELDS': [campo_id_curva, campo_cota],
            'INTERSECT_FIELDS_PREFIX': 'curva_',
            'OUTPUT': 'TEMPORARY_OUTPUT'
        }

        res = processing.run("native:lineintersections", params, context=context, feedback=sub_fb)
        camada_inter = res['OUTPUT']

        total_pts = camada_inter.featureCount()
        feedback_step.pushInfo(f"[Etapa {etapa_num}/4] Total de pontos brutos calculados: {total_pts}")
        if total_pts == 0:
            feedback_step.setProgress(100)
            return None

        nome_f_dren = campo_id_dren
        for f in camada_inter.fields():
            if f.name() == campo_id_dren or campo_id_dren.lower() in f.name().lower():
                nome_f_dren = f.name()
                break

        nome_f_id_curva = f"curva_{campo_id_curva}"
        if camada_inter.fields().indexOf(nome_f_id_curva) == -1:
            for f in camada_inter.fields():
                if 'curva' in f.name().lower() and ('id' in f.name().lower() or 'fid' in f.name().lower()):
                    nome_f_id_curva = f.name()
                    break

        nome_f_cota_curva = f"curva_{campo_cota}"
        if camada_inter.fields().indexOf(nome_f_cota_curva) == -1:
            for f in camada_inter.fields():
                if 'cota' in f.name().lower() or 'elev' in f.name().lower() or 'alt' in f.name().lower():
                    nome_f_cota_curva = f.name()
                    break

        feedback_step.setProgress(70)
        feedback_step.setProgressText(f"Etapa {etapa_num}/4: Agrupando interseções por conflito...")

        grupos = defaultdict(list)
        passo_log = max(100, total_pts // 20)
        for idx_p, feat in enumerate(camada_inter.getFeatures(), 1):
            if idx_p % passo_log == 0 or idx_p == total_pts:
                if feedback_step.isCanceled():
                    return None
                pct_agrup = 70 + int((idx_p / total_pts) * 15)
                feedback_step.setProgress(pct_agrup)
                feedback_step.setProgressText(f"Etapa {etapa_num}/4: Agrupando pontos ({idx_p}/{total_pts})...")

            geom = feat.geometry()
            if not geom or geom.isEmpty():
                continue
            id_d = str(feat[nome_f_dren]) if feat[nome_f_dren] is not None else ""
            id_c = str(feat[nome_f_id_curva]) if (nome_f_id_curva and feat[nome_f_id_curva] is not None) else ""
            cota_v = float(feat[nome_f_cota_curva]) if (nome_f_cota_curva and feat[nome_f_cota_curva] is not None) else None
            chave = (id_d, id_c, cota_v)
            if geom.isMultipart():
                grupos[chave].extend(geom.asMultiPoint())
            else:
                grupos[chave].append(geom.asPoint())

        grupos_conflito = {k: pts for k, pts in grupos.items() if len(pts) >= 2}
        total_conflitos = len(grupos_conflito)
        feedback_step.pushInfo(f"[Etapa {etapa_num}/4] Conflitos identificados (qtd_conexoes >= 2): {total_conflitos} grupos.")

        if total_conflitos == 0:
            feedback_step.setProgress(100)
            return None

        feedback_step.setProgress(85)
        feedback_step.setProgressText(f"Etapa {etapa_num}/4: Carregando geometrias da drenagem...")

        ids_necessarios = {k[0] for k in grupos_conflito.keys()}
        drenagens_geom = {}
        for f in lyr_drenagem.getFeatures():
            v_id = str(f[campo_id_dren])
            if v_id in ids_necessarios:
                drenagens_geom[v_id] = QgsGeometry(f.geometry())

        is_geo = lyr_drenagem.crs().isGeographic()
        precisao = 6 if is_geo else 3

        def formatar_coord(pt_xy):
            return f"{round(pt_xy.x(), precisao)}, {round(pt_xy.y(), precisao)}"

        crs_auth = lyr_drenagem.crs().authid()
        camada_flags = QgsVectorLayer(f"MultiPoint?crs={crs_auth}", self.NOME_CAMADA_FLAGS, "memory")
        dp = camada_flags.dataProvider()
        dp.addAttributes([
            QgsField("fid", QVariant.Int),
            QgsField("id_drenagem", QVariant.String),
            QgsField("id_curva", QVariant.String),
            QgsField("cota_curva", QVariant.Double),
            QgsField("qtd_conexoes", QVariant.Int),
            QgsField("vertice_entrada", QVariant.String),
            QgsField("vertice_saida", QVariant.String)
        ])
        camada_flags.updateFields()

        novas_feicoes = []
        passo_ord = max(1, total_conflitos // 20)
        for idx, ((id_d, id_c, cota_v), lista_pts) in enumerate(grupos_conflito.items(), start=1):
            if idx % passo_ord == 0 or idx == total_conflitos:
                if feedback_step.isCanceled():
                    return None
                pct_ord = 85 + int((idx / total_conflitos) * 15)
                feedback_step.setProgress(pct_ord)
                feedback_step.setProgressText(f"Etapa {etapa_num}/4: Ordenando fluxo no conflito {idx} de {total_conflitos} ({pct_ord}%)...")

            g_dren = drenagens_geom.get(id_d)
            if g_dren and not g_dren.isEmpty():
                pts_ord = sorted(lista_pts, key=lambda p: g_dren.lineLocatePoint(QgsGeometry.fromPointXY(p)))
            else:
                pts_ord = lista_pts

            pt_entrada = pts_ord[0]
            pt_saida = pts_ord[-1]

            f_out = QgsFeature(camada_flags.fields())
            f_out.setGeometry(QgsGeometry.fromMultiPointXY(pts_ord))
            f_out.setAttribute("fid", idx)
            f_out.setAttribute("id_drenagem", id_d)
            f_out.setAttribute("id_curva", id_c)
            f_out.setAttribute("cota_curva", cota_v)
            f_out.setAttribute("qtd_conexoes", len(pts_ord))
            f_out.setAttribute("vertice_entrada", formatar_coord(pt_entrada))
            f_out.setAttribute("vertice_saida", formatar_coord(pt_saida))
            novas_feicoes.append(f_out)

        if novas_feicoes:
            dp.addFeatures(novas_feicoes)
            camada_flags.updateExtents()

            simbolo = QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': '235, 45, 45, 230',
                'size': '2.8',
                'outline_color': 'white',
                'outline_width': '0.5'
            })
            camada_flags.setRenderer(QgsSingleSymbolRenderer(simbolo))
            camada_flags.triggerRepaint()

        feedback_step.setProgress(100)
        return camada_flags

    # =========================================================================
    # ETAPA 2: GERAÇÃO DO ESQUELETO DA CURVA
    # =========================================================================
    def _densificar_geometria(self, geom_sh, passo):
        pts = []
        if geom_sh.geom_type == 'Polygon':
            linhas = [geom_sh.exterior] + list(geom_sh.interiors)
        elif geom_sh.geom_type == 'MultiPolygon':
            linhas = []
            for poly in geom_sh.geoms:
                linhas.append(poly.exterior)
                linhas.extend(poly.interiors)
        elif geom_sh.geom_type in ('LineString', 'LinearRing'):
            linhas = [geom_sh]
        elif geom_sh.geom_type == 'MultiLineString':
            linhas = list(geom_sh.geoms)
        else:
            linhas = []

        for linha in linhas:
            comp = linha.length
            if comp == 0:
                continue
            n_pts = max(3, int(math.ceil(comp / passo)))
            for i in range(n_pts + 1):
                dist = min(comp, i * (comp / float(n_pts)))
                p = linha.interpolate(dist)
                pts.append((p.x, p.y))
        return pts

    def _suavizar_chaikin(self, coords, iteracoes=2):
        if len(coords) < 3:
            return coords
        pts = list(coords)
        for _ in range(iteracoes):
            novos = [pts[0]]
            for i in range(len(pts) - 1):
                p0 = pts[i]
                p1 = pts[i+1]
                q = (0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1])
                r = (0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1])
                novos.extend([q, r])
            novos.append(pts[-1])
            pts = novos
        return pts

    def _obter_vetor_direcao_drenagem(self, pt, linha_dren):
        coords = list(linha_dren.coords)
        if len(coords) < 2:
            return 1.0, 0.0
        m_pt = linha_dren.project(pt)
        dist_acumulada = [linha_dren.project(Point(p)) for p in coords]
        idx_seg = 0
        for i in range(len(dist_acumulada) - 1):
            if dist_acumulada[i] <= m_pt <= dist_acumulada[i+1]:
                idx_seg = i
                break
            elif m_pt > dist_acumulada[i+1]:
                idx_seg = i
        p_a = coords[idx_seg]
        p_b = coords[idx_seg + 1]
        dx = p_b[0] - p_a[0]
        dy = p_b[1] - p_a[1]
        comp = math.hypot(dx, dy)
        if comp == 0:
            return 1.0, 0.0
        return dx / comp, dy / comp

    def _extrair_eixo_central_voronoi(self, poligono, pt_inicio, pt_destino, pt_evitar=None, passo=2.0):
        if not poligono or poligono.is_empty:
            return None

        pts_borda = self._densificar_geometria(poligono, passo)
        if len(pts_borda) < 4:
            return None

        arestas_esqueleto = []
        if TEM_SCIPY:
            vor = Voronoi(pts_borda)
            for p1_idx, p2_idx in vor.ridge_vertices:
                if p1_idx < 0 or p2_idx < 0:
                    continue
                x1, y1 = vor.vertices[p1_idx]
                x2, y2 = vor.vertices[p2_idx]
                aresta = LineString([(x1, y1), (x2, y2)])
                if poligono.buffer(1e-4).contains(aresta):
                    arestas_esqueleto.append(aresta)
        else:
            multiponto_qgs = QgsGeometry.fromMultiPointXY([QgsPointXY(x, y) for x, y in pts_borda])
            envelope_qgs = QgsGeometry.fromWkt(poligono.wkt).boundingBox().buffered(10.0)
            vor_geom = multiponto_qgs.voronoiDiagram(envelope_qgs)
            if vor_geom and not vor_geom.isEmpty():
                sh_vor = shapely.wkb.loads(bytes(vor_geom.asWkb()))
                polys_vor = sh_vor.geoms if sh_vor.geom_type == 'GeometryCollection' else [sh_vor]
                for pv in polys_vor:
                    if pv.geom_type == 'Polygon':
                        anel = pv.exterior
                        for i in range(len(anel.coords) - 1):
                            seg = LineString([anel.coords[i], anel.coords[i+1]])
                            if poligono.buffer(1e-4).contains(seg):
                                arestas_esqueleto.append(seg)

        if not arestas_esqueleto:
            return None

        adj = {}
        def add_aresta(u, v, w):
            adj.setdefault(u, []).append((v, w))
            adj.setdefault(v, []).append((u, w))

        p_evitar_xy = (round(pt_evitar.x, 3), round(pt_evitar.y, 3)) if pt_evitar else None

        for seg in arestas_esqueleto:
            c = list(seg.coords)
            for i in range(len(c) - 1):
                u = (round(c[i][0], 3), round(c[i][1], 3))
                v = (round(c[i+1][0], 3), round(c[i+1][1], 3))
                if u != v:
                    w = math.hypot(u[0] - v[0], u[1] - v[1])
                    if p_evitar_xy:
                        d_u = math.hypot(u[0] - p_evitar_xy[0], u[1] - p_evitar_xy[1])
                        d_v = math.hypot(v[0] - p_evitar_xy[0], v[1] - p_evitar_xy[1])
                        if d_u < 8.0 or d_v < 8.0:
                            w += 5000.0
                    add_aresta(u, v, w)

        if not adj:
            return None

        nodes = list(adj.keys())
        p_ini_xy = (round(pt_inicio.x, 3), round(pt_inicio.y, 3))
        p_fim_xy = (round(pt_destino.x, 3), round(pt_destino.y, 3))

        cx = sum(n[0] for n in nodes) / len(nodes)
        cy = sum(n[1] for n in nodes) / len(nodes)
        vx = cx - p_ini_xy[0]
        vy = cy - p_ini_xy[1]
        comp_v = math.hypot(vx, vy)
        u_dir = (vx / comp_v, vy / comp_v) if comp_v > 0 else (0.0, 1.0)

        for u in list(adj.keys()):
            dx_u = u[0] - p_ini_xy[0]
            dy_u = u[1] - p_ini_xy[1]
            proj_u = dx_u * u_dir[0] + dy_u * u_dir[1]
            dist_u = math.hypot(dx_u, dy_u)
            if proj_u < -0.5 and dist_u < 30.0:
                novas_viz = []
                for viz, w in adj[u]:
                    novas_viz.append((viz, w + 10000.0))
                adj[u] = novas_viz

        cands_start = []
        for n in nodes:
            dx = n[0] - p_ini_xy[0]
            dy = n[1] - p_ini_xy[1]
            dist = math.hypot(dx, dy)
            if dist < 1e-4:
                continue
            cos_ang = (dx * u_dir[0] + dy * u_dir[1]) / dist
            if cos_ang > 0.15:
                score = dist / (cos_ang ** 0.8)
                cands_start.append((score, n))

        if cands_start:
            cands_start.sort(key=lambda item: item[0])
            start_node = cands_start[0][1]
        else:
            start_node = min(nodes, key=lambda n: (n[0] - p_ini_xy[0])**2 + (n[1] - p_ini_xy[1])**2)

        cands_end = nodes
        if p_evitar_xy:
            cands_end = [n for n in nodes if math.hypot(n[0] - p_evitar_xy[0], n[1] - p_evitar_xy[1]) > 5.0]
            if not cands_end:
                cands_end = nodes

        end_node = min(cands_end, key=lambda n: (n[0] - p_fim_xy[0])**2 + (n[1] - p_fim_xy[1])**2)

        dist_start = math.hypot(p_ini_xy[0] - start_node[0], p_ini_xy[1] - start_node[1])
        dist_end   = math.hypot(p_fim_xy[0] - end_node[0], p_fim_xy[1] - end_node[1])
        add_aresta(p_ini_xy, start_node, dist_start)
        add_aresta(p_fim_xy, end_node, dist_end)

        visitados = set()
        componentes = []
        for no in adj:
            if no not in visitados:
                comp = []
                fila_c = [no]
                visitados.add(no)
                while fila_c:
                    curr = fila_c.pop(0)
                    comp.append(curr)
                    for viz, _ in adj.get(curr, []):
                        if viz not in visitados:
                            visitados.add(viz)
                            fila_c.append(viz)
                componentes.append(comp)

        if len(componentes) > 1:
            comp_principal = next((c for c in componentes if p_ini_xy in c), componentes[0])
            for outro_comp in componentes:
                if outro_comp is comp_principal:
                    continue
                melhor_par = None
                menor_d = float('inf')
                for n1 in comp_principal:
                    for n2 in outro_comp:
                        d = (n1[0] - n2[0])**2 + (n1[1] - n2[1])**2
                        if d < menor_d:
                            menor_d = d
                            melhor_par = (n1, n2, math.sqrt(d))
                if melhor_par:
                    add_aresta(melhor_par[0], melhor_par[1], melhor_par[2])

        distancias = {p_ini_xy: 0.0}
        anterior = {}
        fila = [(0.0, p_ini_xy)]

        caminho_encontrado = False
        while fila:
            d_atual, u = heapq.heappop(fila)
            if u == p_fim_xy:
                caminho_encontrado = True
                break
            if d_atual > distancias.get(u, float('inf')):
                continue

            for viz, w in adj.get(u, []):
                d_nova = d_atual + w
                if d_nova < distancias.get(viz, float('inf')):
                    distancias[viz] = d_nova
                    anterior[viz] = u
                    heapq.heappush(fila, (d_nova, viz))

        if not caminho_encontrado:
            if end_node in anterior:
                caminho = [p_fim_xy]
                curr = end_node
                while curr in anterior:
                    caminho.append(curr)
                    curr = anterior[curr]
                caminho.append(p_ini_xy)
                caminho.reverse()
            else:
                return None
        else:
            caminho = []
            curr = p_fim_xy
            while curr in anterior:
                caminho.append(curr)
                curr = anterior[curr]
            caminho.append(p_ini_xy)
            caminho.reverse()

        if len(caminho) < 2:
            return None

        linha_temp = LineString(caminho)
        try:
            l_simp = linha_temp.simplify(self.TOLERANCIA_SUAVIZACAO, preserve_topology=True)
            if l_simp and l_simp.is_valid and l_simp.length > 0:
                caminho = list(l_simp.coords)
        except Exception:
            pass

        coords_suaves = self._suavizar_chaikin(caminho, iteracoes=self.ITERACOES_CHAIKIN)
        return LineString(coords_suaves)

    def _construir_poligono_braco(self, g_pts, id_dren_str, cota_num, lyr_dren, lyr_curvas):
        campo_id_dren_lyr = next((f.name() for f in lyr_dren.fields() if f.name().lower() in ['id', 'id_drenagem', 'fid', 'id_objeto']), lyr_dren.fields()[0].name())
        campo_id_curva_lyr = next((f.name() for f in lyr_curvas.fields() if f.name().lower() in ['id_curva', 'id', 'fid']), lyr_curvas.fields()[0].name())
        campo_cota_curva_lyr = next((f.name() for f in lyr_curvas.fields() if f.name().lower() in ['cota', 'elevation', 'altitude', 'z']), None)

        f_dren = None
        if id_dren_str and campo_id_dren_lyr:
            expr = f'"{campo_id_dren_lyr}" = \'{id_dren_str}\''
            cands = list(lyr_dren.getFeatures(QgsFeatureRequest().setFilterExpression(expr)))
            if cands:
                f_dren = cands[0]
        if not f_dren:
            cands = [f for f in lyr_dren.getFeatures(QgsFeatureRequest().setFilterRect(g_pts.boundingBox().buffered(10.0))) if f.geometry().intersects(g_pts)]
            if cands:
                f_dren = cands[0]

        if not f_dren:
            return None, ""

        sh_dren = shapely.wkb.loads(bytes(f_dren.geometry().asWkb()))
        linha_dren = sh_dren.geoms[0] if sh_dren.geom_type == 'MultiLineString' else sh_dren

        req_curva = QgsFeatureRequest().setFilterRect(g_pts.boundingBox().buffered(50.0))
        if cota_num is not None and campo_cota_curva_lyr:
            req_curva.setFilterExpression(f'"{campo_cota_curva_lyr}" = {cota_num}')

        curvas_cands = []
        for fc in lyr_curvas.getFeatures(req_curva):
            gc = fc.geometry()
            if gc and gc.intersects(g_pts.buffer(2.0, 4)):
                sh_c = shapely.wkb.loads(bytes(gc.asWkb()))
                id_curva_c = str(fc[campo_id_curva_lyr]) if campo_id_curva_lyr else str(fc.id())
                if sh_c.geom_type == 'MultiLineString':
                    for sub in sh_c.geoms:
                        curvas_cands.append({"geom": sub, "id_curva": id_curva_c})
                elif sh_c.geom_type == 'LineString':
                    curvas_cands.append({"geom": sh_c, "id_curva": id_curva_c})

        if not curvas_cands:
            return None, ""

        sh_pts = shapely.wkb.loads(bytes(g_pts.asWkb()))
        pts_lista = list(sh_pts.geoms) if sh_pts.geom_type == 'MultiPoint' else [sh_pts]

        melhor_curva = max(curvas_cands, key=lambda c: sum(1 for pt in pts_lista if c["geom"].distance(pt) < 1.0))
        linha_curva = melhor_curva["geom"]
        id_curva_encontrada = melhor_curva["id_curva"]

        coords_curva = list(linha_curva.coords)
        n_total = len(coords_curva)
        if n_total < 3:
            return None, id_curva_encontrada

        pts_ordenados = sorted(pts_lista, key=lambda p: linha_dren.project(p))
        p_primeiro = pts_ordenados[0]
        p_ultimo = pts_ordenados[-1]

        def idx_mais_prox(pt_sh, coords):
            return min(range(len(coords)), key=lambda i: (coords[i][0] - pt_sh.x)**2 + (coords[i][1] - pt_sh.y)**2)

        idx_primeiro = idx_mais_prox(p_primeiro, coords_curva)
        idx_ultimo = idx_mais_prox(p_ultimo, coords_curva)

        delta_k = idx_ultimo - idx_primeiro
        qtd_lado1 = abs(delta_k)
        if qtd_lado1 == 0:
            return None, id_curva_encontrada

        sentido1 = 1 if delta_k > 0 else -1
        sentido2 = -sentido1

        coords_braco1 = [coords_curva[idx_primeiro + i * sentido1] for i in range(qtd_lado1 + 1)]
        ux_fim, uy_fim = self._obter_vetor_direcao_drenagem(p_ultimo, linha_dren)
        nx, ny = -uy_fim, ux_fim
        pt_u = Point(coords_braco1[-1])

        linha_imag = LineString([
            (pt_u.x - nx * self.COMPRIMENTO_LINHA_IMAGINARIA, pt_u.y - ny * self.COMPRIMENTO_LINHA_IMAGINARIA),
            (pt_u.x + nx * self.COMPRIMENTO_LINHA_IMAGINARIA, pt_u.y + ny * self.COMPRIMENTO_LINHA_IMAGINARIA)
        ])

        coords_braco2 = [coords_curva[idx_primeiro]]
        ponto_corte = None
        curr_idx = idx_primeiro
        max_passos = min(n_total, qtd_lado1 * 3)

        for passo in range(1, max_passos):
            next_idx = curr_idx + sentido2
            if next_idx < 0 or next_idx >= n_total:
                break
            seg = LineString([Point(coords_curva[curr_idx]), Point(coords_curva[next_idx])])
            if passo > 2 and seg.intersects(linha_imag):
                inter = seg.intersection(linha_imag)
                ponto_corte = inter if inter.geom_type == 'Point' else (inter.geoms[0] if inter.geom_type == 'MultiPoint' else Point(coords_curva[next_idx]))
                coords_braco2.append((ponto_corte.x, ponto_corte.y))
                break
            else:
                coords_braco2.append(coords_curva[next_idx])
                curr_idx = next_idx

        if ponto_corte is None:
            coords_braco2 = [coords_curva[idx_primeiro]]
            for passo in range(1, qtd_lado1 + 1):
                next_idx = idx_primeiro + (passo * sentido2)
                if 0 <= next_idx < n_total:
                    coords_braco2.append(coords_curva[next_idx])
                else:
                    break

        anel = coords_braco2[::-1] + coords_braco1[1:] + [coords_braco2[-1]]
        poly = Polygon(anel)
        if not poly.is_valid:
            poly = poly.buffer(0)
        return poly, id_curva_encontrada

    # =========================================================================
    # ETAPA 3: ATUALIZAÇÃO DA DRENAGEM (EMENDA DO ESQUELETO)
    # =========================================================================
    def _extrair_pontos(self, geom):
        if geom is None or geom.isEmpty():
            return []
        if geom.isMultipart():
            pontos = []
            for parte in geom.asMultiPolyline():
                pontos.extend(parte)
            return pontos
        return geom.asPolyline()

    def _fatiar_polyline(self, pontos, dist_inicio, dist_fim):
        if not pontos or len(pontos) < 2:
            return []
        dist_total = 0.0
        for i in range(len(pontos) - 1):
            dist_total += math.sqrt(pontos[i].sqrDist(pontos[i + 1]))
        dist_inicio = max(0.0, min(dist_inicio, dist_total))
        dist_fim = max(0.0, min(dist_fim, dist_total))
        if dist_fim - dist_inicio < 1e-4:
            return []

        resultado = []
        dist_acum = 0.0
        adicionando = False
        if dist_inicio == 0.0:
            resultado.append(pontos[0])
            adicionando = True

        for i in range(len(pontos) - 1):
            p1 = pontos[i]
            p2 = pontos[i + 1]
            comp_seg = math.sqrt(p1.sqrDist(p2))
            if comp_seg < 1e-9:
                continue
            seg_ini = dist_acum
            seg_fim = dist_acum + comp_seg

            if not adicionando and seg_ini <= dist_inicio <= seg_fim:
                t = (dist_inicio - seg_ini) / comp_seg
                pt_ini = QgsPointXY(p1.x() + t * (p2.x() - p1.x()), p1.y() + t * (p2.y() - p1.y()))
                resultado.append(pt_ini)
                adicionando = True

            if adicionando and seg_ini <= dist_fim <= seg_fim:
                t = (dist_fim - seg_ini) / comp_seg
                pt_fim = QgsPointXY(p1.x() + t * (p2.x() - p1.x()), p1.y() + t * (p2.y() - p1.y()))
                if not resultado or resultado[-1].sqrDist(pt_fim) > 1e-8:
                    resultado.append(pt_fim)
                adicionando = False
                break

            if adicionando:
                if not resultado or resultado[-1].sqrDist(p2) > 1e-8:
                    resultado.append(p2)
            dist_acum = seg_fim

        if adicionando:
            if not resultado or resultado[-1].sqrDist(pontos[-1]) > 1e-8:
                resultado.append(pontos[-1])
        return resultado

    def _encontrar_pontos_conexao(self, geom_orig, geom_sub):
        pts_sub = self._extrair_pontos(geom_sub)
        if not pts_sub or len(pts_sub) < 2:
            return None
        comp_orig = geom_orig.length()
        comp_sub = geom_sub.length()

        pts_inter = []
        try:
            inter = geom_orig.intersection(geom_sub)
            if inter and not inter.isEmpty():
                tf = QgsWkbTypes.flatType(inter.wkbType())
                if tf == QgsWkbTypes.Point:
                    pts_inter.append(inter.asPoint())
                elif tf == QgsWkbTypes.MultiPoint:
                    pts_inter.extend(inter.asMultiPoint())
                elif tf in (QgsWkbTypes.LineString, QgsWkbTypes.MultiLineString):
                    pts_inter.extend(self._extrair_pontos(inter))
                elif inter.isMultipart():
                    for p in inter.asGeometryCollection():
                        if p.type() == QgsWkbTypes.PointGeometry:
                            pts_inter.extend(p.asMultiPoint() if p.isMultipart() else [p.asPoint()])
        except Exception:
            pts_inter = []

        pts_unicos = []
        for pt in pts_inter:
            if not any(pt.sqrDist(u) < 1e-4 for u in pts_unicos):
                pts_unicos.append(pt)

        if len(pts_unicos) >= 2:
            dists_orig = [geom_orig.lineLocatePoint(QgsGeometry.fromPointXY(p)) for p in pts_unicos]
            d_min = min(dists_orig)
            d_max = max(dists_orig)

            dists_sub = [geom_sub.lineLocatePoint(QgsGeometry.fromPointXY(p)) for p in pts_unicos]
            s_min = min(dists_sub)
            s_max = max(dists_sub)
            if s_max - s_min > 0.05 and (s_min > 0.05 or s_max < comp_sub - 0.05):
                pts_aparados = self._fatiar_polyline(pts_sub, s_min, s_max)
                if pts_aparados and len(pts_aparados) >= 2:
                    pts_sub = pts_aparados
        else:
            d_inicio = geom_orig.lineLocatePoint(QgsGeometry.fromPointXY(pts_sub[0]))
            d_fim = geom_orig.lineLocatePoint(QgsGeometry.fromPointXY(pts_sub[-1]))
            d_min = min(d_inicio, d_fim)
            d_max = max(d_inicio, d_fim)

        if abs(d_max - d_min) < 1e-3:
            return None

        pt_no_dmin = geom_orig.interpolate(d_min).asPoint()
        if pts_sub[0].sqrDist(pt_no_dmin) > pts_sub[-1].sqrDist(pt_no_dmin):
            pts_sub.reverse()

        return d_min, d_max, pts_sub

    def _emendar_trecho_ajustado(self, geom_orig, geom_sub):
        pts_orig = self._extrair_pontos(geom_orig)
        if not pts_orig or len(pts_orig) < 2:
            return None
        dados = self._encontrar_pontos_conexao(geom_orig, geom_sub)
        if not dados:
            return None
        d_min, d_max, pts_sub = dados
        comp_orig = geom_orig.length()

        pts_prefixo = []
        if d_min > 0.05:
            pts_prefixo = self._fatiar_polyline(pts_orig, 0.0, d_min)
        pts_sufixo = []
        if d_max < (comp_orig - 0.05):
            pts_sufixo = self._fatiar_polyline(pts_orig, d_max, comp_orig)

        if pts_prefixo:
            pts_sub[0] = pts_prefixo[-1]
        if pts_sufixo:
            pts_sub[-1] = pts_sufixo[0]

        pontos_combinados = []
        if pts_prefixo:
            pontos_combinados.extend(pts_prefixo)
            pontos_combinados.extend(pts_sub[1:])
        else:
            pontos_combinados.extend(pts_sub)
        if pts_sufixo:
            pontos_combinados.extend(pts_sufixo[1:])

        return pontos_combinados, d_min, d_max

    def _construir_geometria_final(self, pontos_xy, camada_alvo, geom_original, d_min, d_max):
        tipo_alvo = camada_alvo.wkbType()
        possui_z = QgsWkbTypes.hasZ(tipo_alvo)
        eh_multi = QgsWkbTypes.isMultiType(tipo_alvo)

        if possui_z:
            z_inicio = 0.0
            z_fim = 0.0
            try:
                pt_ini_orig = geom_original.interpolate(d_min)
                pt_fim_orig = geom_original.interpolate(d_max)
                if hasattr(pt_ini_orig.constGet(), 'z'):
                    z_inicio = pt_ini_orig.constGet().z()
                if hasattr(pt_fim_orig.constGet(), 'z'):
                    z_fim = pt_fim_orig.constGet().z()
            except Exception:
                pass

            comprimento_total = 0.0
            distancias = [0.0]
            for i in range(1, len(pontos_xy)):
                dist = math.sqrt(pontos_xy[i].sqrDist(pontos_xy[i - 1]))
                comprimento_total += dist
                distancias.append(comprimento_total)

            pontos_3d = []
            for i, pt in enumerate(pontos_xy):
                fator = (distancias[i] / comprimento_total) if comprimento_total > 0 else 0.0
                z = z_inicio + fator * (z_fim - z_inicio)
                pontos_3d.append(QgsPoint(pt.x(), pt.y(), z))

            linha = QgsLineString(pontos_3d)
            geom = QgsGeometry(linha)
        else:
            geom = QgsGeometry.fromPolylineXY(pontos_xy)

        if eh_multi and not geom.isMultipart():
            geom.convertToMultiType()
        return geom

    # =========================================================================
    # PIPELINE PRINCIPAL DO ALGORITMO
    # =========================================================================
    def processAlgorithm(self, parameters, context, feedback):
        lyr_drenagem = self.parameterAsVectorLayer(parameters, self.PARAM_DRENAGEM, context)
        lyr_curva = self.parameterAsVectorLayer(parameters, self.PARAM_CURVA, context)

        exec_etapa_1 = self.parameterAsBoolean(parameters, self.PARAM_ETAPA_1, context)
        exec_etapa_2 = self.parameterAsBoolean(parameters, self.PARAM_ETAPA_2, context)

        if self.PARAM_ETAPA_3 in parameters:
            exec_etapa_3 = self.parameterAsBoolean(parameters, self.PARAM_ETAPA_3, context)
        elif "PARAM_ATUALIZAR_DRENAGEM" in parameters:
            exec_etapa_3 = self.parameterAsBoolean(parameters, "PARAM_ATUALIZAR_DRENAGEM", context)
        else:
            exec_etapa_3 = True

        exec_etapa_4 = self.parameterAsBoolean(parameters, self.PARAM_ETAPA_4, context)

        apenas_selecionadas = self.parameterAsBoolean(parameters, self.PARAM_APENAS_SELECIONADAS, context)
        caminho_backup = self.parameterAsString(parameters, self.PARAM_BACKUP_GPKG, context)

        if not (exec_etapa_1 or exec_etapa_2 or exec_etapa_3 or exec_etapa_4):
            feedback.reportError("Nenhum processo foi selecionado para execução. Marque ao menos uma das 4 etapas.")
            return {}

        if not lyr_drenagem or not lyr_drenagem.isValid():
            feedback.reportError("Camada de drenagem inválida!")
            return {}
        if (exec_etapa_1 or exec_etapa_2 or exec_etapa_4) and (not lyr_curva or not lyr_curva.isValid()):
            feedback.reportError("Camada de curvas de nível inválida!")
            return {}

        total_passos = 4
        feedback_multi = QgsProcessingMultiStepFeedback(total_passos, feedback)

        # ---------------------------------------------------------------------
        # PRÉ-VALIDAÇÃO DO BACKUP SE ATUALIZAR DRENAGEM (ETAPA 3) ESTIVER ATIVO
        # ---------------------------------------------------------------------
        if exec_etapa_3:
            feedback.pushInfo("[Segurança] Atualização da drenagem solicitada. Verificando backup...")
            caminho_backup_efetivo = self._executar_backup(lyr_drenagem, caminho_backup, feedback)
            if not caminho_backup_efetivo:
                feedback.reportError("Processo cancelado: O backup da drenagem original não pôde ser realizado.")
                return {"status": "cancelado_backup"}

        # ---------------------------------------------------------------------
        # VERIFICAÇÃO DE ESQUELETO PRÉ-EXISTENTE NO PROJETO
        # ---------------------------------------------------------------------
        camada_esq_existente = self._obter_camada_projeto(self.NOME_CAMADA_ESQUELETO)
        tem_esqueleto_valido = (
            camada_esq_existente is not None
            and camada_esq_existente.isValid()
            and camada_esq_existente.featureCount() > 0
        )

        # Regra de início automático: se a camada esqueleto_curva_bbox já existir no projeto e a Etapa 3 for executada,
        # pula as etapas 1 e 2 automaticamente, aproveitando os esqueletos existentes e indo direto para a atualização.
        pular_por_esqueleto = (
            tem_esqueleto_valido
            and exec_etapa_3
            and (exec_etapa_1 or exec_etapa_2)
        )

        camada_flags = None
        novos_esqueletos = []

        # ---------------------------------------------------------------------
        # ETAPA 1/4: DETECÇÃO DE CONFLITOS (flags_drenagem_curva)
        # ---------------------------------------------------------------------
        feedback_multi.setCurrentStep(0)
        if not exec_etapa_1:
            feedback.pushInfo("[Etapa 1/4] Desmarcada pelo usuário. Pulando...")
            feedback_multi.setProgress(100)
        elif pular_por_esqueleto:
            feedback.pushInfo(
                f"[Etapa 1/4] A camada de esqueletos '{self.NOME_CAMADA_ESQUELETO}' já existe no projeto "
                f"com {camada_esq_existente.featureCount()} feições. Pulando cálculo inicial de conflitos..."
            )
            feedback_multi.setProgress(100)
        else:
            feedback_multi.setProgressText("Etapa 1/4: Identificando conflitos entre hidrografia e curvas...")
            camada_flags_proj = self._obter_camada_projeto(self.NOME_CAMADA_FLAGS)

            if (exec_etapa_2 or exec_etapa_3 or exec_etapa_4) and camada_flags_proj and camada_flags_proj.isValid() and camada_flags_proj.featureCount() > 0:
                feedback.pushInfo(f"[Etapa 1/4] A camada '{self.NOME_CAMADA_FLAGS}' já existe no projeto com {camada_flags_proj.featureCount()} feições. Pulando cálculo inicial...")
                camada_flags = camada_flags_proj
                feedback_multi.setProgress(100)
            else:
                feedback.pushInfo("[Etapa 1/4] Calculando interseções drenagem x curva...")
                camada_flags = self._calcular_intersecoes(lyr_drenagem, lyr_curva, feedback_multi, context, etapa_num=1)
                if not camada_flags or camada_flags.featureCount() == 0:
                    feedback.pushInfo("[Sucesso] Não foram encontrados conflitos (nenhum trecho com 2 ou mais toques na mesma curva).")
                    if not (exec_etapa_2 or exec_etapa_3 or exec_etapa_4):
                        return {"status": "sem_conflitos"}
                else:
                    self._remover_camada_por_nome(self.NOME_CAMADA_FLAGS)
                    QgsProject.instance().addMapLayer(camada_flags)
                    self._organizar_camada_no_grupo(camada_flags)
                feedback_multi.setProgress(100)

        if feedback.isCanceled():
            return {}

        # ---------------------------------------------------------------------
        # ETAPA 2/4: GERAÇÃO DOS ESQUELETOS (esqueleto_curva_bbox)
        # ---------------------------------------------------------------------
        feedback_multi.setCurrentStep(1)
        feicoes_flags = []

        if not exec_etapa_2:
            feedback.pushInfo("[Etapa 2/4] Desmarcada pelo usuário. Pulando...")
            feedback_multi.setProgress(100)
        elif pular_por_esqueleto:
            feedback.pushInfo(
                f"[Etapa 2/4] Camada '{self.NOME_CAMADA_ESQUELETO}' já existe no projeto "
                f"com {camada_esq_existente.featureCount()} feições. Pulando geração e aproveitando esqueletos existentes..."
            )
            feedback_multi.setProgress(100)
        else:
            feedback_multi.setProgressText("Etapa 2/4: Gerando eixos centrais dos vales (esqueleto)...")
            if not camada_flags:
                camada_flags = self._obter_camada_projeto(self.NOME_CAMADA_FLAGS)

            if not camada_flags or not camada_flags.isValid() or camada_flags.featureCount() == 0:
                feedback.reportError(f"A camada '{self.NOME_CAMADA_FLAGS}' não foi encontrada ou está vazia no projeto. Execute a Etapa 1 primeiro.")
                return {}

            if apenas_selecionadas and camada_flags.selectedFeatureCount() > 0:
                feicoes_flags = camada_flags.selectedFeatures()
                feedback.pushInfo(f"[Etapa 2/4] Processando {len(feicoes_flags)} flag(s) SELECIONADA(S).")
            else:
                feicoes_flags = list(camada_flags.getFeatures())
                feedback.pushInfo(f"[Etapa 2/4] Processando TODAS as {len(feicoes_flags)} flag(s) da camada.")

        total_flags = len(feicoes_flags)
        if exec_etapa_2 and not pular_por_esqueleto and total_flags == 0:
            feedback.pushInfo("[Aviso] Nenhuma flag para processar na Etapa 2.")

        # Mapeamento de campos da camada de flags
        campo_id_dren_inter = next((f.name() for f in camada_flags.fields() if f.name().lower() in ['id_drenagem', 'id_dren']), None)
        campo_id_curva_inter = next((f.name() for f in camada_flags.fields() if f.name().lower() in ['id_curva', 'fid_curva']), None)
        campo_cota_inter = next((f.name() for f in camada_flags.fields() if f.name().lower() in ['cota_curva', 'cota', 'elevation', 'altitude', 'z']), None)
        campo_fid_inter = next((f.name() for f in camada_flags.fields() if f.name().lower() in ['fid']), None)
        campo_id_inter = next((f.name() for f in camada_flags.fields() if f.name().lower() in ['id_flags_drenagem_curva', 'id_intersections_qtd_maior_1', 'id']), camada_flags.fields()[0].name())

        # Criar camada temporária de esqueleto
        crs_auth = lyr_drenagem.crs().authid()
        camada_esqueleto = QgsVectorLayer(f"MultiLineString?crs={crs_auth}", self.NOME_CAMADA_ESQUELETO, "memory")
        dp_esq = camada_esqueleto.dataProvider()
        dp_esq.addAttributes([
            QgsField("id_curva", QVariant.String),
            QgsField("id_drenagem", QVariant.String),
            QgsField("id_flags_drenagem_curva", QVariant.String),
            QgsField("comprimento_m", QVariant.Double),
            QgsField("origem_braco", QVariant.String),
            QgsField("status", QVariant.String)
        ])
        camada_esqueleto.updateFields()

        lyr_bracos = self._obter_camada_projeto("bracos_curva_nivel_corte90")
        campo_id_dren_lyr = next((f.name() for f in lyr_drenagem.fields() if f.name().lower() in ['id', 'id_drenagem', 'fid']), lyr_drenagem.fields()[0].name())

        novos_esqueletos = []
        for idx_f, feat_inter in enumerate(feicoes_flags, 1):
            if feedback.isCanceled():
                break

            pct = int((idx_f / total_flags) * 100)
            feedback_multi.setProgress(pct)
            feedback_multi.setProgressText(f"Etapa 2/4: Gerando esqueleto da flag {idx_f} de {total_flags} ({pct}%)...")

            geom_pts = feat_inter.geometry()
            if not geom_pts or geom_pts.isEmpty():
                continue

            val_fid = str(feat_inter[campo_fid_inter]) if campo_fid_inter and feat_inter[campo_fid_inter] is not None else str(feat_inter.id())
            val_id_inter = str(feat_inter[campo_id_inter]) if campo_id_inter and feat_inter[campo_id_inter] is not None else val_fid
            val_id_dren = str(feat_inter[campo_id_dren_inter]) if campo_id_dren_inter and feat_inter[campo_id_dren_inter] is not None else ""
            val_id_curva = str(feat_inter[campo_id_curva_inter]) if campo_id_curva_inter and feat_inter[campo_id_curva_inter] is not None else ""
            val_cota = float(feat_inter[campo_cota_inter]) if campo_cota_inter and feat_inter[campo_cota_inter] is not None else None

            sh_pts = shapely.wkb.loads(bytes(geom_pts.asWkb()))
            pts_lista = list(sh_pts.geoms) if sh_pts.geom_type == 'MultiPoint' else [sh_pts]

            f_dren_sel = None
            if val_id_dren and campo_id_dren_lyr:
                expr = f'"{campo_id_dren_lyr}" = \'{val_id_dren}\''
                cands_d = list(lyr_drenagem.getFeatures(QgsFeatureRequest().setFilterExpression(expr)))
                if cands_d:
                    f_dren_sel = cands_d[0]
            if not f_dren_sel:
                cands_d = [f for f in lyr_drenagem.getFeatures(QgsFeatureRequest().setFilterRect(geom_pts.boundingBox().buffered(30.0))) if f.geometry().intersects(geom_pts)]
                if cands_d:
                    f_dren_sel = cands_d[0]

            pt_inicio = None
            pt_fim_errado = None
            linha_dren_fluxo = None

            if f_dren_sel:
                sh_d = shapely.wkb.loads(bytes(f_dren_sel.geometry().asWkb()))
                l_d = sh_d.geoms[0] if sh_d.geom_type == 'MultiLineString' else sh_d
                pts_ord = sorted(pts_lista, key=lambda p: l_d.project(p))
                m0 = l_d.project(pts_ord[0])
                m1 = l_d.project(pts_ord[-1])
                if m0 > m1:
                    linha_dren_fluxo = LineString(list(l_d.coords)[::-1])
                    pt_inicio = pts_ord[-1]
                    pt_fim_errado = pts_ord[0]
                else:
                    linha_dren_fluxo = l_d
                    pt_inicio = pts_ord[0]
                    pt_fim_errado = pts_ord[-1]
            else:
                idx_e = feat_inter.fields().indexOf("vertice_entrada")
                idx_s = feat_inter.fields().indexOf("vertice_saida")
                if idx_e != -1 and idx_s != -1 and feat_inter[idx_e] and feat_inter[idx_s]:
                    try:
                        ce = [float(x.strip()) for x in str(feat_inter[idx_e]).split(',')]
                        cs = [float(x.strip()) for x in str(feat_inter[idx_s]).split(',')]
                        pt_inicio = Point(ce[0], ce[1])
                        pt_fim_errado = Point(cs[0], cs[1])
                    except Exception:
                        pass
                if not pt_inicio or not pt_fim_errado:
                    pt_inicio = pts_lista[0]
                    pt_fim_errado = pts_lista[-1]

            pt_conexao_jusante = None
            if linha_dren_fluxo:
                m_fim_errado = linha_dren_fluxo.project(pt_fim_errado)
                coords_d = list(linha_dren_fluxo.coords)
                candidatos_v = []
                for v_c in coords_d:
                    m_v = linha_dren_fluxo.project(Point(v_c))
                    delta = m_v - m_fim_errado
                    if delta > 1.5:
                        candidatos_v.append((delta, Point(v_c)))
                if candidatos_v:
                    candidatos_v.sort(key=lambda item: item[0])
                    pt_conexao_jusante = candidatos_v[0][1]
                else:
                    pt_conexao_jusante = Point(coords_d[-1])
            else:
                pt_conexao_jusante = pt_fim_errado

            poly_alvo = None
            origem_desc = ""

            if lyr_bracos and lyr_bracos.isValid():
                cands_braco = []
                if lyr_bracos.fields().indexOf("fid_inter") != -1:
                    try:
                        cands_braco = list(lyr_bracos.getFeatures(QgsFeatureRequest().setFilterExpression(f'"fid_inter" = {int(val_fid)}')))
                    except Exception:
                        pass
                if not cands_braco:
                    cands_braco = [f for f in lyr_bracos.getFeatures(QgsFeatureRequest().setFilterRect(geom_pts.boundingBox().buffered(10.0))) if f.geometry().intersects(geom_pts)]
                if cands_braco:
                    poly_alvo = shapely.wkb.loads(bytes(cands_braco[0].geometry().asWkb()))
                    origem_desc = "Camada bracos_curva_nivel_corte90"

            if not poly_alvo or poly_alvo.is_empty:
                poly_calc, id_c_calc = self._construir_poligono_braco(geom_pts, val_id_dren, val_cota, lyr_drenagem, lyr_curva)
                if poly_calc and not poly_calc.is_empty:
                    poly_alvo = poly_calc
                    origem_desc = "Construído pela reentrância da curva"
                    if not val_id_curva and id_c_calc:
                        val_id_curva = id_c_calc

            if not poly_alvo or poly_alvo.is_empty:
                continue

            eixo_central = self._extrair_eixo_central_voronoi(
                poly_alvo,
                pt_inicio=pt_inicio,
                pt_destino=pt_conexao_jusante,
                pt_evitar=pt_fim_errado,
                passo=self.PASSO_DENSIFICACAO
            )

            if not eixo_central or eixo_central.is_empty:
                continue

            if eixo_central.geom_type == 'LineString':
                eixo_central = MultiLineString([eixo_central])

            geom_out = QgsGeometry()
            geom_out.fromWkb(shapely.wkb.dumps(eixo_central))
            if not geom_out.isMultipart():
                geom_out.convertToMultiType()

            comp_total = round(eixo_central.length, 2)
            f_nova = QgsFeature(camada_esqueleto.fields())
            f_nova.setGeometry(geom_out)
            f_nova.setAttribute("id_curva", str(val_id_curva))
            f_nova.setAttribute("id_drenagem", str(val_id_dren))
            f_nova.setAttribute("id_flags_drenagem_curva", str(val_id_inter))
            f_nova.setAttribute("comprimento_m", comp_total)
            f_nova.setAttribute("origem_braco", origem_desc)
            f_nova.setAttribute("status", "Eixo Central com Conexão Suave")
            novos_esqueletos.append(f_nova)

        if novos_esqueletos:
            self._remover_camada_por_nome(self.NOME_CAMADA_ESQUELETO)
            dp_esq.addFeatures(novos_esqueletos)
            camada_esqueleto.updateExtents()

            simbolo = QgsLineSymbol.createSimple({
                'color': '200, 0, 40, 255',
                'width': '1.3',
                'line_style': 'solid',
                'capstyle': 'round',
                'joinstyle': 'round'
            })
            camada_esqueleto.setRenderer(QgsSingleSymbolRenderer(simbolo))
            camada_esqueleto.triggerRepaint()
            QgsProject.instance().addMapLayer(camada_esqueleto)
            self._organizar_camada_no_grupo(camada_esqueleto)
            feedback.pushInfo(f"[Etapa 2/4] Sucesso: {len(novos_esqueletos)} esqueleto(s) gerado(s) em '{self.NOME_CAMADA_ESQUELETO}'.")
        else:
            feedback.pushWarning("[Etapa 2/4] Não foi possível derivar esqueletos para as flags processadas.")

        if feedback.isCanceled():
            return {}

        # ---------------------------------------------------------------------
        # ETAPA 3/4: ATUALIZAÇÃO DA DRENAGEM
        # ---------------------------------------------------------------------
        feedback_multi.setCurrentStep(2)
        total_atualizados = 0

        if not exec_etapa_3:
            feedback.pushInfo("[Etapa 3/4] Desmarcada pelo usuário. Pulando etapa de atualização da drenagem...")
            feedback_multi.setProgress(100)
        else:
            feedback_multi.setProgressText("Etapa 3/4: Atualizando feições na camada de drenagem...")

            feicoes_esqueleto_alvo = []
            if novos_esqueletos:
                feicoes_esqueleto_alvo = novos_esqueletos
            else:
                camada_esq_proj = self._obter_camada_projeto(self.NOME_CAMADA_ESQUELETO)
                if camada_esq_proj and camada_esq_proj.isValid() and camada_esq_proj.featureCount() > 0:
                    if apenas_selecionadas and camada_esq_proj.selectedFeatureCount() > 0:
                        feicoes_esqueleto_alvo = list(camada_esq_proj.selectedFeatures())
                        feedback.pushInfo(f"[Etapa 3/4] Usando {len(feicoes_esqueleto_alvo)} esqueleto(s) SELECIONADO(S) da camada '{self.NOME_CAMADA_ESQUELETO}' existente no projeto.")
                    else:
                        feicoes_esqueleto_alvo = list(camada_esq_proj.getFeatures())
                        feedback.pushInfo(f"[Etapa 3/4] Usando todas as {len(feicoes_esqueleto_alvo)} feições da camada '{self.NOME_CAMADA_ESQUELETO}' existente no projeto.")

            if not feicoes_esqueleto_alvo:
                feedback.pushWarning(f"[Etapa 3/4] Nenhum esqueleto encontrado (nem gerado nem na camada '{self.NOME_CAMADA_ESQUELETO}') para atualizar a drenagem.")
                feedback_multi.setProgress(100)
            else:
                campo_id_dren_alvo = next((f.name() for f in lyr_drenagem.fields() if f.name().lower() in ['id', 'id_drenagem', 'id_trecho_drenagem', 'fid', 'pk']), None)

                mapa_drenagem = {}
                geometrias_atuais = {}
                for feat in lyr_drenagem.getFeatures():
                    fid = feat.id()
                    geometrias_atuais[fid] = feat.geometry()
                    if campo_id_dren_alvo:
                        val = feat[campo_id_dren_alvo]
                        if val is not None:
                            mapa_drenagem[str(val).strip()] = fid
                            try:
                                mapa_drenagem[int(val)] = fid
                            except Exception:
                                pass
                    mapa_drenagem[str(fid)] = fid
                    mapa_drenagem[fid] = fid

                if not lyr_drenagem.isEditable():
                    lyr_drenagem.startEditing()

                total_esq = len(feicoes_esqueleto_alvo)
                for idx_e, feat_sub in enumerate(feicoes_esqueleto_alvo, 1):
                    if feedback.isCanceled():
                        break

                    pct = int((idx_e / total_esq) * 100)
                    feedback_multi.setProgress(pct)
                    feedback_multi.setProgressText(f"Etapa 3/4: Emendando trecho {idx_e} de {total_esq} na drenagem ({pct}%)...")

                    id_dren_val = None
                    for campo_esq in ['id_drenagem', 'id_dren', 'id_trecho_drenagem', 'id']:
                        idx_f_esq = feat_sub.fields().indexOf(campo_esq)
                        if idx_f_esq != -1:
                            v = feat_sub[idx_f_esq]
                            if v is not None and str(v).strip():
                                id_dren_val = v
                                break

                    if not id_dren_val:
                        continue

                    chave = str(id_dren_val).strip()
                    fid_alvo = mapa_drenagem.get(chave)
                    if fid_alvo is None:
                        try:
                            fid_alvo = mapa_drenagem.get(int(chave))
                        except Exception:
                            pass

                    if fid_alvo is None:
                        continue

                    geom_orig = geometrias_atuais[fid_alvo]
                    geom_sub = feat_sub.geometry()

                    resultado_emenda = self._emendar_trecho_ajustado(geom_orig, geom_sub)
                    if not resultado_emenda:
                        continue

                    pontos_comb, d_min, d_max = resultado_emenda
                    geom_final = self._construir_geometria_final(pontos_comb, lyr_drenagem, geom_orig, d_min, d_max)

                    if geom_final and not geom_final.isEmpty():
                        lyr_drenagem.changeGeometry(fid_alvo, geom_final)
                        geometrias_atuais[fid_alvo] = geom_final
                        total_atualizados += 1

                lyr_drenagem.commitChanges()
                lyr_drenagem.triggerRepaint()
                feedback.pushInfo(f"[Etapa 3/4] Concluída! {total_atualizados} de {total_esq} trecho(s) de drenagem foram atualizados.")
                feedback_multi.setProgress(100)

        if feedback.isCanceled():
            return {}

        # ---------------------------------------------------------------------
        # ETAPA 4/4: REVALIDAÇÃO E SALDO FINAL DE CONFLITOS
        # ---------------------------------------------------------------------
        feedback_multi.setCurrentStep(3)
        qtd_final = 0

        if not exec_etapa_4:
            feedback.pushInfo("[Etapa 4/4] Desmarcada pelo usuário. Pulando etapa de revalidação...")
            feedback_multi.setProgress(100)
        else:
            feedback_multi.setProgressText("Etapa 4/4: Recalculando saldo final de flags remanescentes...")
            feedback.pushInfo("[Etapa 4/4] Substituindo camada de flags anterior pelas pendências reais pós-processamento...")

            self._remover_camada_por_nome(self.NOME_CAMADA_FLAGS)

            camada_flags_final = self._calcular_intersecoes(lyr_drenagem, lyr_curva, feedback_multi, context, etapa_num=4)
            if camada_flags_final and camada_flags_final.featureCount() > 0:
                qtd_final = camada_flags_final.featureCount()
                QgsProject.instance().addMapLayer(camada_flags_final)
                self._organizar_camada_no_grupo(camada_flags_final)
                feedback.pushWarning(f"\n[Atenção] Saldo final: {qtd_final} flag(s) remanescente(s) identificada(s) para conferência/ajuste manual.")
            else:
                feedback.pushInfo("\n[Excelente!] Todas as interseções conflituosas foram eliminadas com sucesso. Saldo final = 0 flags!")

            feedback_multi.setProgress(100)

        # NOTA: A camada esqueleto_curva_bbox permanece intacta e adicionada no projeto!
        feedback.pushInfo("=" * 70)
        feedback.pushInfo(f"[CONCLUÍDO] Pipeline executado com sucesso!")
        feedback.pushInfo(f"  - Eixos centrais novos gerados: {len(novos_esqueletos)}")
        feedback.pushInfo(f"  - Trechos de drenagem alterados: {total_atualizados}")
        feedback.pushInfo(f"  - Flags finais restantes (pendências): {qtd_final}")
        feedback.pushInfo("=" * 70)

        return {
            "esqueletos_gerados": len(novos_esqueletos),
            "drenagens_atualizadas": total_atualizados,
            "flags_restantes": qtd_final
        }

    def name(self):
        return "Corrigir_Drenagem"

    def displayName(self):
        return "Corrigir Drenagem v1.0.0"

    def group(self):
        return "🐸 SAPO"

    def groupId(self):
        return "sapo"

    def createInstance(self):
        return Sapo_Corrigir_Drenagem()

    def shortHelpString(self):
        return QCoreApplication.translate(
            "Corrigir_Drenagem",
            f"Versão: {self.VERSAO}\n\n"
            "Algoritmo modular e autossuficiente para identificação, traçado de esqueleto e correção geométrica "
            "de conflitos entre trechos de hidrografia e curvas de nível:\n\n"
            "CONTROLE DE ETAPAS (CHECKBOXES):\n"
            "- Cada processo possui um checkbox independente (todos marcados por padrão).\n"
            "- Permite rodar o fluxo completo de ponta a ponta ou apenas etapas específicas isoladas.\n\n"
            "1. Etapa 1: Interseção Drenagem x Curva\n"
            "   - Identifica trechos com 2 ou mais toques na mesma curva de nível (conflitos de reentrância).\n"
            "   - Gera a camada 'flags_drenagem_curva' com ordenamento do fluxo (montante -> jusante).\n"
            "   - Se a camada 'flags_drenagem_curva' já existir no projeto, pula o cálculo inicial.\n\n"
            "2. Etapa 2: Traçado do Esqueleto Central\n"
            "   - Gera os eixos centrais dos vales na camada temporária 'esqueleto_curva_bbox'.\n"
            "   - A camada gerada é mantida no projeto e NÃO é excluída no final.\n"
            "   - Atalho inteligente: Se 'esqueleto_curva_bbox' já existir no projeto, pula automaticamente as Etapas 1 e 2 "
            "e inicia diretamente na atualização da hidrografia.\n\n"
            "3. Etapa 3: Atualização da Camada de Drenagem\n"
            "   - Pré-valida e executa o backup de segurança em GeoPackage (.gpkg).\n"
            "   - Emenda os eixos da camada 'esqueleto_curva_bbox' diretamente na camada de hidrografia.\n\n"
            "4. Etapa 4: Revalidação e Saldo de Flags\n"
            "   - Substitui a camada de flags anterior recalculando as interseções na drenagem atualizada.\n"
            "   - Apresenta o saldo final de pendências reais para conferência/edição manual.\n\n"
            "Barra de Progresso:\n"
            "Acompanhamento em tempo real para cada feição processada nas etapas ativas."
        )

