# -*- coding: utf-8 -*-

"""
Python mirrors of the Tairu Maps Firestore entities the plugin exchanges.

Field names and encodings replicate the Flutter app exactly
(lib/common/record_model.dart Record.toFirestore() and
lib/common/firestore_entity.dart toFirestoreBase()):
- geometryPoints/geometryBounds are JSON *strings* of [{"la","lo","ts"}, ...];
- timestamps (createdAt/lastModified/eventDateTime) are epoch milliseconds ints;
- colors are ARGB ints.
"""

import calendar
import json
import re
import time
import uuid
from dataclasses import dataclass, field


# Record type / subtype / situation catalogs (from record_model.dart enums).
# Keys are the enum names stored in Firestore; values are display labels.

RECORD_TYPES = {
    'pessoa': 'Pessoa',
    'local': 'Local',
    'equipamento': 'Equipamento',
    'veiculo': 'Veículo',
    'acao': 'Ação',
    'ocorrencia': 'Ocorrência',
    'trilha': 'Trilha',
    'pontoDeInteresse': 'Ponto de Interesse',
    'desenho': 'Desenho',
}

RECORD_SUBTYPES = {
    # Pessoa
    'usuario': 'Usuário', 'pessoa': 'Pessoa', 'outraPessoa': 'Outro',
    # Local
    'residencia': 'Residência', 'empresa': 'Empresa', 'comercio': 'Comércio',
    'industria': 'Indústria', 'rural': 'Rural', 'ponte': 'Ponte',
    'trajeto': 'Trajeto', 'rota': 'Rota', 'outroLocal': 'Outro',
    # Local — natureza
    'cachoeira': 'Cachoeira', 'mirante': 'Mirante', 'gruta': 'Gruta / Caverna',
    'fonteDeAgua': "Fonte d'Água", 'porteira': 'Porteira / Acesso',
    'bifurcacao': 'Bifurcação de Trilha', 'travessiaDeRio': 'Travessia de Rio',
    'areaDeAcampamento': 'Área de Acampamento', 'abrigo': 'Abrigo / Rancho',
    'pico': 'Pico / Cume',
    # Local — operacional
    'baseOperacional': 'Base Operacional', 'pontoDeControle': 'Ponto de Controle',
    'areaEmbargada': 'Área Embargada', 'areaDePreservacao': 'Área de Preservação',
    # Equipamento
    'draga': 'Draga', 'motor': 'Motor', 'escavadeira': 'Escavadeira',
    'trator': 'Trator', 'britador': 'Britador', 'gerador': 'Gerador',
    'motobomba': 'Motobomba', 'motosserra': 'Motosserra', 'outroEquipamento': 'Outro',
    # Equipamento adicionais
    'drone': 'Drone / VANT', 'armadilhaFotografica': 'Armadilha Fotográfica',
    'barraca': 'Barraca de Camping', 'kitPrimeirosSocorros': 'Kit de Primeiros Socorros',
    'compressor': 'Compressor',
    # Veículo
    'motocicleta': 'Motocicleta', 'carro': 'Carro', 'caminhonete': 'Caminhonete',
    'barco': 'Barco', 'quadriciclo': 'Quadriciclo', 'outroVeiculo': 'Outro',
    # Veículo adicionais
    'bicicleta': 'Bicicleta / MTB', 'caiaqueCanoa': 'Caiaque / Canoa',
    'aeronave': 'Aeronave / Avião', 'caminhao': 'Caminhão',
    # Ação
    'busca': 'Busca', 'rastreamento': 'Rastreamento', 'encontro': 'Encontro',
    'pernoite': 'Pernoite', 'descanso': 'Descanso', 'outraAcao': 'Outra',
    # Ação — operacional
    'vistoria': 'Vistoria', 'patrulhamento': 'Patrulhamento', 'embargo': 'Embargo',
    'coletaDeEvidencias': 'Coleta de Evidências',
    # Ação — recreativo
    'trilhagem': 'Trilhagem', 'campismo': 'Campismo', 'escalada': 'Escalada / Rapel',
    'canoagem': 'Canoagem / Caiaque',
    # Ocorrência
    'desmatamento': 'Desmatamento', 'incendioFlorestal': 'Incêndio Florestal',
    'garimpIlegal': 'Garimpo Ilegal', 'pescaIlegal': 'Pesca Ilegal',
    'cacaIlegal': 'Caça Ilegal', 'extracaoIlegal': 'Extração Ilegal',
    'descarte': 'Descarte Irregular', 'construcaoIrregular': 'Construção Irregular',
    'outraOcorrencia': 'Outra Ocorrência',
    # Trilha
    'trilhaPedestre': 'Trilha Pedestre', 'trilhaMTB': 'Trilha MTB',
    'trilhaCavalo': 'Trilha Equestre', 'rotaDeRio': 'Rota de Rio / Canoagem',
    'trilhaMista': 'Trilha Mista',
    # Ponto de Interesse
    'paisagem': 'Paisagem', 'floraPoI': 'Flora', 'faunaPoI': 'Fauna',
    'perigoPoI': 'Ponto de Perigo', 'artefato': 'Artefato / Sítio',
    'referenciaPoI': 'Referência', 'outroPoI': 'Outro',
    # Desenho
    'desenhoPonto': 'Ponto', 'desenhoLinha': 'Linha',
    'desenhoPoligono': 'Polígono', 'desenhoCirculo': 'Círculo',
}

SUBTYPES_BY_TYPE = {
    'pessoa': ['usuario', 'pessoa', 'outraPessoa'],
    'local': ['residencia', 'empresa', 'comercio', 'industria', 'rural',
              'ponte', 'trajeto', 'rota', 'outroLocal',
              'cachoeira', 'mirante', 'gruta', 'fonteDeAgua', 'porteira',
              'bifurcacao', 'travessiaDeRio', 'areaDeAcampamento', 'abrigo', 'pico',
              'baseOperacional', 'pontoDeControle', 'areaEmbargada', 'areaDePreservacao'],
    'equipamento': ['draga', 'motor', 'escavadeira', 'trator', 'britador',
                    'gerador', 'motobomba', 'motosserra', 'outroEquipamento',
                    'drone', 'armadilhaFotografica', 'barraca', 'kitPrimeirosSocorros', 'compressor'],
    'veiculo': ['motocicleta', 'carro', 'caminhonete', 'barco', 'quadriciclo', 'outroVeiculo',
                'bicicleta', 'caiaqueCanoa', 'aeronave', 'caminhao'],
    'acao': ['busca', 'rastreamento', 'encontro', 'pernoite', 'descanso', 'outraAcao',
             'vistoria', 'patrulhamento', 'embargo', 'coletaDeEvidencias',
             'trilhagem', 'campismo', 'escalada', 'canoagem'],
    'ocorrencia': ['desmatamento', 'incendioFlorestal', 'garimpIlegal', 'pescaIlegal',
                   'cacaIlegal', 'extracaoIlegal', 'descarte', 'construcaoIrregular',
                   'outraOcorrencia'],
    'trilha': ['trilhaPedestre', 'trilhaMTB', 'trilhaCavalo', 'rotaDeRio', 'trilhaMista'],
    'pontoDeInteresse': ['paisagem', 'floraPoI', 'faunaPoI', 'perigoPoI', 'artefato',
                         'referenciaPoI', 'outroPoI'],
    'desenho': ['desenhoPonto', 'desenhoLinha', 'desenhoPoligono', 'desenhoCirculo'],
}

SITUATIONS_BY_TYPE = {
    'pessoa': ['Livre', 'Outra'],
    'local': ['Ativo', 'Inativo', 'Incorreto', 'Não Localizado', 'Outro'],
    'equipamento': ['Localizado', 'Arrecadado', 'Apreendido', 'Inutilizado', 'Não Localizado'],
    'veiculo': ['Localizado', 'Arrecadado', 'Apreendido', 'Inutilizado', 'Não Localizado'],
    'acao': ['Concluída', 'Em Andamento', 'Pendente', 'Cancelada'],
    'ocorrencia': ['Registrada', 'Em Apuração', 'Auto Lavrado', 'Encerrada', 'Arquivada'],
    'trilha': ['Transitável', 'Com Restrições', 'Intransitável', 'Desconhecida'],
    'pontoDeInteresse': ['A Visitar', 'Visitado', 'Recomendado', 'Evitar'],
    'desenho': ['Ativo', 'Inativo'],
}

GEOMETRY_TYPES = ('none', 'point', 'line', 'polygon', 'circle')


def now_millis():
    return int(time.time() * 1000)


_RFC3339_RE = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})'
    r'(?:\.(\d+))?(?:[Zz]|([+-])(\d{2}):?(\d{2}))?$')


def parse_millis(value):
    """Epoch milliseconds from any timestamp representation found in Firestore.

    The app WRITES DateTime (stored as Firestore Timestamp → RFC 3339 string
    through the REST codec) but READS both ints and Timestamps
    (FirestoreEntity.parseLastModified), so legacy data mixes the two. The
    plugin writes int millis and must read everything.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = _RFC3339_RE.match(value.strip())
        if match:
            y, mo, d, h, mi, s = (int(match.group(i)) for i in range(1, 7))
            frac = match.group(7) or ''
            ms = int((frac + '000')[:3]) if frac else 0
            epoch = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
            if match.group(8):
                offset = int(match.group(9)) * 3600 + int(match.group(10)) * 60
                epoch -= offset if match.group(8) == '+' else -offset
            return epoch * 1000 + ms
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def points_to_json(points, ts=None):
    """[(lat, lon), ...] -> compact geometryPoints JSON string."""
    ts = ts if ts is not None else now_millis()
    return json.dumps([{'la': la, 'lo': lo, 'ts': ts} for la, lo in points],
                      separators=(',', ':'))


def bounds_json_from_points(points, ts=None):
    """NW + SE corners as the app's LayerFeature.boundsFromPoints produces."""
    if not points:
        return None
    ts = ts if ts is not None else now_millis()
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    nw = {'la': max(lats), 'lo': min(lons), 'ts': ts}
    se = {'la': min(lats), 'lo': max(lons), 'ts': ts}
    return json.dumps([nw, se], separators=(',', ':'))


def parse_points_json(points_json):
    """geometryPoints JSON string -> [(lat, lon), ...]; raises on bad input."""
    items = json.loads(points_json)
    return [(float(p['la']), float(p['lo'])) for p in items]


@dataclass
class TairuMap:
    map_id: str
    nome: str = ''
    descricao: str = ''
    status: str = 'active'
    owner_id: str = ''
    store_in_cloud: bool = False
    integrants: dict = field(default_factory=dict)
    admin_ids: list = field(default_factory=list)
    user_ids: list = field(default_factory=list)
    integrant_id_list: list = field(default_factory=list)
    tairudb_remote_files: list = field(default_factory=list)
    active_alert_count: int = 0
    has_emergency_alert: bool = False
    plan_version: str = 'online'
    is_deleted: bool = False

    @classmethod
    def from_fields(cls, map_id, d):
        integrants = _decode_role_map(d.get('integrants'))
        integrant_id_list = d.get('integrantIdList') or []
        if not integrants and integrant_id_list:
            for user_id in integrant_id_list:
                if isinstance(user_id, str) and user_id:
                    integrants[user_id] = 'owner' if user_id == (d.get('ownerId') or '') else 'user'
        return cls(
            map_id=map_id,
            nome=d.get('nome') or '',
            descricao=d.get('descricao') or '',
            status=d.get('status') or 'active',
            owner_id=d.get('ownerId') or '',
            store_in_cloud=bool(d.get('storeInCloud') or False),
            integrants=integrants,
            admin_ids=d.get('adminIds') or [],
            user_ids=d.get('userIds') or [],
            integrant_id_list=integrant_id_list,
            tairudb_remote_files=d.get('tairuDBRemoteFiles') or [],
            active_alert_count=_safe_int(d.get('activeAlertCount')),
            has_emergency_alert=bool(d.get('hasEmergencyAlert') or False),
            plan_version=d.get('planVersion') or 'online',
            is_deleted=bool(d.get('isDeleted') or False),
        )

    def role_for(self, uid):
        """'owner' | 'admin' | 'user' | None — mirrors the rules helpers."""
        if uid == self.owner_id:
            return 'owner'
        role = (self.integrants or {}).get(uid)
        if role in ('owner', 'admin', 'user'):
            return role
        if uid in (self.admin_ids or []):
            return 'admin'
        if uid in (self.user_ids or []):
            return 'user'
        return None

    def member_count(self):
        if self.integrants:
            return len(self.integrants)
        if self.integrant_id_list:
            return len(self.integrant_id_list)
        members = set(self.admin_ids or []) | set(self.user_ids or [])
        if self.owner_id:
            members.add(self.owner_id)
        return len(members)

    def can_edit_files(self, uid):
        """Raster upload + map-doc patch require owner/admin (rules)."""
        return self.role_for(uid) in ('owner', 'admin')

    def role_label(self, uid):
        return {'owner': 'Proprietário', 'admin': 'Admin', 'user': 'Membro'}.get(self.role_for(uid), '—')


def _decode_role_map(value):
    if not isinstance(value, dict):
        return {}
    decoded = {}
    for key, role in value.items():
        if isinstance(key, str) and key:
            decoded[key] = role if role in ('owner', 'admin', 'user') else 'admin'
    return decoded


def _safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class TairuRecord:
    record_id: str
    nome: str = ''
    descricao: str = ''
    situation: str = ''
    endereco: str = ''
    tipo_registro: str = 'local'
    sub_tipo: str = 'outroLocal'
    fotos: str = '[]'
    owner: str = ''
    size: float = 0.0
    plate_tag: str = ''
    brand: str = ''
    model: str = ''
    year: int = 0
    color: str = ''
    value_estimate: float = 0.0
    event_date_time: int = 0            # epoch millis
    geometry_type: str = 'none'
    geometry_points_json: str = None    # JSON string (app encoding)
    geometry_bounds_json: str = None
    circle_radius: float = None         # meters
    geometry_size: float = None
    geometry_color_value: int = None    # ARGB
    geometry_background_color_value: int = None
    is_deleted: bool = False
    created_by: str = ''
    created_at: int = 0                 # epoch millis
    last_modified: int = 0              # epoch millis

    @classmethod
    def from_fields(cls, record_id, d):
        def _f(key, default=None):
            v = d.get(key)
            return default if v is None else v

        def _num(key, cast, default):
            try:
                return cast(d.get(key))
            except (TypeError, ValueError):
                return default

        def _opt_num(key, cast):
            try:
                value = d.get(key)
                return None if value is None else cast(value)
            except (TypeError, ValueError):
                return None

        rec = cls(
            record_id=record_id or _f('recordId', ''),
            nome=_f('nome', ''),
            descricao=_f('descricao', ''),
            situation=_f('situation', ''),
            endereco=_f('endereco', ''),
            tipo_registro=_f('tipoRegistro', 'local'),
            sub_tipo=_f('subTipo', 'outroLocal'),
            fotos=_f('fotos', '[]'),
            owner=_f('owner', ''),
            size=_num('size', float, 0.0),
            plate_tag=_f('plateTag', ''),
            brand=_f('brand', ''),
            model=_f('model', ''),
            year=_num('year', int, 0),
            color=_f('color', ''),
            value_estimate=_num('valueEstimate', float, 0.0),
            event_date_time=parse_millis(_f('eventDateTime', 0)),
            geometry_type=_f('geometryType', 'none') or 'none',
            geometry_points_json=_f('geometryPoints'),
            geometry_bounds_json=_f('geometryBounds'),
            circle_radius=_opt_num('circleRadius', float),
            geometry_size=_opt_num('geometrySize', float),
            geometry_color_value=_opt_num('geometryColorValue', int),
            geometry_background_color_value=_opt_num('geometryBackgroundColorValue', int),
            is_deleted=bool(_f('isDeleted', False)),
            created_by=_f('createdBy', ''),
            created_at=parse_millis(_f('createdAt', 0)),
            last_modified=parse_millis(_f('lastModified', 0)),
        )
        # Legacy records: geometry only in deprecated la/lo fields
        if not rec.geometry_points_json and (d.get('la') or d.get('lo')):
            try:
                rec.geometry_points_json = points_to_json(
                    [(float(d.get('la') or 0.0), float(d.get('lo') or 0.0))],
                    ts=rec.last_modified or now_millis(),
                )
                if rec.geometry_type == 'none':
                    rec.geometry_type = 'point'
            except (TypeError, ValueError):
                pass
        return rec

    def points(self):
        """[(lat, lon), ...] or [] when absent/unparseable."""
        if not self.geometry_points_json:
            return []
        try:
            return parse_points_json(self.geometry_points_json)
        except (ValueError, KeyError, TypeError):
            return []

    def to_fields(self):
        """Full field dict matching Record.toFirestore() (serverTimestamp is
        added as a transform by the Firestore write builders, not here)."""
        fields = {
            'recordId': self.record_id,
            'nome': self.nome,
            'descricao': self.descricao,
            'situation': self.situation,
            'endereco': self.endereco,
            'tipoRegistro': self.tipo_registro,
            'fotos': self.fotos,
            'subTipo': self.sub_tipo,
            'owner': self.owner,
            'size': float(self.size),
            'plateTag': self.plate_tag,
            'brand': self.brand,
            'model': self.model,
            'year': int(self.year),
            'color': self.color,
            'valueEstimate': float(self.value_estimate),
            'eventDateTime': parse_millis(self.event_date_time),
            'isDeleted': self.is_deleted,
            'lastModified': parse_millis(self.last_modified),
            'createdBy': self.created_by,
            'createdAt': parse_millis(self.created_at),
        }
        if self.geometry_type is not None:
            fields['geometryType'] = self.geometry_type
        if self.geometry_points_json:
            fields['geometryPoints'] = self.geometry_points_json
        if self.geometry_bounds_json:
            fields['geometryBounds'] = self.geometry_bounds_json
        if self.circle_radius is not None:
            fields['circleRadius'] = float(self.circle_radius)
        if self.geometry_size is not None:
            fields['geometrySize'] = float(self.geometry_size)
        if self.geometry_color_value is not None:
            fields['geometryColorValue'] = int(self.geometry_color_value)
        if self.geometry_background_color_value is not None:
            fields['geometryBackgroundColorValue'] = int(self.geometry_background_color_value)
        return fields

    @staticmethod
    def new_id():
        return str(uuid.uuid4())
