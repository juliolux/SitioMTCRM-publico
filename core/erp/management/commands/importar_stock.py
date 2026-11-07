"""Importa el stock completo desde los CSVs de gestión.

Lee dos archivos exportados del sistema de gestión (Bejerman/Tango/etc.):
  - SINÓNIMOS: base + sinónimo + proveedor + modelo + última compra
  - UTILIDADES: margen de ganancia (UTIL1)

Los junta por la columna "Nro" (id interno estable y único entre ambos archivos)
y carga en el modelo Product.

Supuestos (confirmados por el cliente):
  - Lista1 y PCosto INCLUYEN IVA -> se dividen por (1 + alícuota) para guardar neto.
  - El separador es ";" y la codificación es Latin-1.
  - Las celdas numéricas traen apóstrofe de Excel (ej: '790..., '0) -> se limpian.

Modo de uso:
  python manage.py importar_stock \
      --sinonimos "path/SINONIMOS.CSV" \
      --utilidades "path/UTILIDADES.CSV" \
      --company "LA FERIA DEL OUTLET SAS" \
      --cuit "30719461987" \
      --apply        # sin --apply solo muestra el informe (modo prueba)
"""
import csv
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.erp.models import Category, Company, Product, Supplier

DOS = Decimal('0.01')
SIN_RUBRO = 'SIN RUBRO'


def clean_cell(v):
    """Limpia una celda: strip y quita el apóstrofe inicial de Excel."""
    if v is None:
        return ''
    s = str(v).strip()
    if s.startswith("'"):
        s = s[1:]
    return s.strip()


def to_decimal(v):
    """Convierte a Decimal tolerando apóstrofes, $, % y coma decimal."""
    s = clean_cell(v)
    if not s:
        return None
    s = s.replace('$', '').replace('%', '').replace(' ', '')
    if ',' in s and '.' in s:
        # formato 1.234,56 -> 1234.56
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def parse_iva(v, default=Decimal('0.21')):
    d = to_decimal(v)
    if d is None:
        return default
    if d > 1:
        d = d / 100
    return d


def load_csv(path):
    rows = []
    with open(path, encoding='latin-1', newline='') as f:
        reader = csv.reader(f, delimiter=';')
        header = next(reader)
        for r in reader:
            if len(r) != len(header):
                continue  # fila mal formada
            rows.append({h: clean_cell(r[i]) for i, h in enumerate(header)})
    return header, rows


class Command(BaseCommand):
    help = 'Importa stock desde CSVs SINÓNIMOS + UTILIDADES (merge por Nro).'

    def add_arguments(self, parser):
        parser.add_argument('--sinonimos', required=True, help='CSV de SINÓNIMOS')
        parser.add_argument('--utilidades', required=True, help='CSV de UTILIDADES')
        parser.add_argument('--company', required=True, help='Nombre de la empresa')
        parser.add_argument('--cuit', default='', help='CUIT de la empresa (opcional)')
        parser.add_argument('--apply', action='store_true',
                            help='Aplicar cambios en la BD. Sin esta bandera es modo prueba.')

    def handle(self, *args, **opts):
        apply_mode = opts['apply']

        self.stdout.write('Leyendo archivos...')
        h1, rows_sin = load_csv(opts['sinonimos'])
        h2, rows_uti = load_csv(opts['utilidades'])
        self.stdout.write(f'  SINÓNIMOS: {len(rows_sin)} filas, {len(h1)} columnas')
        self.stdout.write(f'  UTILIDADES: {len(rows_uti)} filas, {len(h2)} columnas')

        # Merge por Nro (id interno único y estable entre archivos)
        uti_by_nro = {}
        for r in rows_uti:
            nro = clean_cell(r.get('Nro'))
            if nro:
                uti_by_nro[nro] = r
        self.stdout.write(f'  Merge por Nro: {len(uti_by_nro)} claves en UTILIDADES')

        # ---- Construir registros normalizados ----
        products = []          # lista de dicts listos para cargar
        rubros = Counter()     # nombre -> cantidad
        marcas = Counter()
        proveedores = Counter()
        sin_rubro = 0
        sin_marca = 0
        sin_proveedor = 0
        sin_sinonimo = 0
        negativos = 0
        names_used = set()
        names_deduped = 0
        iva_counter = Counter()
        parse_errors = []

        for r in rows_sin:
            code = clean_cell(r.get('Codigo'))
            name = clean_cell(r.get('Descripcion'))
            rubro = clean_cell(r.get('Rubro'))
            marca = clean_cell(r.get('Marca'))
            sinonimo = clean_cell(r.get('Sinonimo'))
            proveedor = clean_cell(r.get('Proveedor'))
            lista1 = to_decimal(r.get('Lista1'))
            pcosto = to_decimal(r.get('PCosto'))
            iva_rate = parse_iva(r.get('IVA'))
            stock = to_decimal(r.get('StockActual'))
            nro = clean_cell(r.get('Nro'))

            # margen desde UTILIDADES
            margen = Decimal('0')
            urow = uti_by_nro.get(nro)
            if urow is not None:
                m = to_decimal(urow.get('UTIL1'))
                if m is not None:
                    margen = m
            # el campo margin_percentage es DecimalField(max_digits=5) -> máx 999.99
            if margen > Decimal('999.99'):
                margen = Decimal('999.99')

            if not code and not name:
                continue

            iva_counter[str(iva_rate)] += 1

            # --- stock: negativos a 0 ---
            if stock is None:
                stock = Decimal('0')
            if stock < 0:
                negativos += 1
                stock = Decimal('0')

            # --- precios con IVA incluido -> neto ---
            factor = Decimal('1') + iva_rate
            pvp_net = None
            pvp_final = None
            cost_net = None
            if lista1 is not None:
                pvp_final = lista1
                pvp_net = (lista1 / factor).quantize(DOS, ROUND_HALF_UP)
            if pcosto is not None:
                cost_net = (pcosto / factor).quantize(DOS, ROUND_HALF_UP)

            # --- nombre único ---
            base_name = name
            if name in names_used:
                name = f"{base_name} ({code})"
                names_deduped += 1
                while name in names_used:
                    name = name + "'"
            if len(name) > 150:
                name = name[:150]
            names_used.add(name)

            # --- rubro / marca / proveedor ---
            if rubro:
                rubros[rubro] += 1
            else:
                sin_rubro += 1
                rubros[SIN_RUBRO] += 1
            if marca:
                marcas[marca] += 1
            else:
                sin_marca += 1
            if proveedor:
                proveedores[proveedor] += 1
            else:
                sin_proveedor += 1
            if not sinonimo:
                sin_sinonimo += 1

            products.append({
                'code': code,
                'external_code': sinonimo or None,
                'name': name,
                'base_name': base_name,
                'rubro': rubro,
                'marca': marca,
                'proveedor': proveedor,
                'cost_price': cost_net,
                'pvp': pvp_net,
                'pvp_final': pvp_final,
                'iva_rate': iva_rate,
                'margin_percentage': margen,
                'stock': stock,
            })

        # ---- Informe ----
        self.stdout.write('')
        self.stdout.write('=' * 60)
        self.stdout.write('   INFORME DE IMPORTACIÓN' + (' (MODO PRUEBA)' if not apply_mode else ' (APLICANDO)'))
        self.stdout.write('=' * 60)
        self.stdout.write(f'Empresa: {opts["company"]}  CUIT: {opts["cuit"] or "-"}')
        self.stdout.write(f'Productos totales: {len(products)}')
        self.stdout.write('')
        self.stdout.write('--- Categorías (rubros) ---')
        self.stdout.write(f'  Rubros únicos: {len(rubros)} (incluye "{SIN_RUBRO}")')
        self.stdout.write(f'  Productos sin rubro -> "{SIN_RUBRO}": {sin_rubro}')
        self.stdout.write('--- Marcas ---')
        self.stdout.write(f'  Marcas únicas: {len(marcas)}')
        self.stdout.write(f'  Productos sin marca: {sin_marca}')
        self.stdout.write('--- Proveedores ---')
        self.stdout.write(f'  Proveedores únicos: {len(proveedores)}')
        self.stdout.write(f'  Productos sin proveedor: {sin_proveedor}')
        for p, c in proveedores.most_common(5):
            self.stdout.write(f'    {p}: {c}')
        self.stdout.write('--- Datos ---')
        self.stdout.write(f'  Nombres repetidos (diferenciados con código): {names_deduped}')
        self.stdout.write(f'  Stock negativo -> 0: {negativos}')
        self.stdout.write(f'  Sin sinónimo (external_code): {sin_sinonimo}')
        self.stdout.write(f'  IVA: {dict(iva_counter)}')

        # Precios ejemplo
        self.stdout.write('')
        self.stdout.write('--- Conversión de precios (IVA incluido -> neto) ---')
        shown = 0
        for p in products:
            if p['pvp'] is not None and p['pvp_final'] is not None and shown < 4:
                self.stdout.write(f'  "{p["base_name"][:40]}" final=${p["pvp_final"]} -> neto=${p["pvp"]} (IVA {p["iva_rate"]*100:.1f}%)')
                shown += 1

        if not apply_mode:
            self.stdout.write('')
            self.stdout.write('MODO PRUEBA: no se modificó la base de datos.')
            self.stdout.write('Para aplicar: agregar --apply al comando.')
            return

        # ---- Aplicar ----
        self.stdout.write('')
        self.stdout.write('Aplicando cambios en la base de datos...')
        self._apply(opts, products, rubros, marcas, proveedores)

    def _apply(self, opts, products, rubros, marcas, proveedores):
        from django.db import IntegrityError

        with transaction.atomic():
            # Empresa
            company, _ = Company.objects.get_or_create(
                name__iexact=opts['company'],
                defaults={'name': opts['company'], 'cuit': opts['cuit'] or None},
            )
            self.stdout.write(f'  Empresa: {company.name} (id={company.id})')

            # Proveedores
            sup_map = {}
            for name in proveedores:
                sup, _ = Supplier.objects.get_or_create(
                    name__iexact=name, company=company,
                    defaults={'name': name},
                )
                sup_map[name] = sup
            self.stdout.write(f'  Proveedores: {len(sup_map)}')

            # Categorías (rubro) y marcas
            # OJO: unique_together es (company, name). Un rubro y una marca con el
            # mismo nombre no pueden coexistir -> desambiguamos la marca.
            cat_map = {}
            rubro_upper = {n.upper() for n in rubros}
            for name in rubros:
                cat, _ = Category.objects.get_or_create(
                    name=name.upper(), company=company,
                    defaults={'category_type': 'category'},
                )
                cat_map[name] = cat
                cat_map[name.upper()] = cat
            for name in marcas:
                brand_name = name.upper()
                if brand_name in rubro_upper:
                    brand_name = f'{brand_name} (MARCA)'
                cat, _ = Category.objects.get_or_create(
                    name=brand_name, company=company, category_type='brand',
                    defaults={'category_type': 'brand'},
                )
                cat_map[('brand', name)] = cat
            self.stdout.write(f'  Categorías: {len(rubros)} | Marcas: {len(marcas)}')

            # Productos
            code_counts = Counter(p['code'] for p in products if p['code'])
            dup_codes = {c for c, n in code_counts.items() if n > 1}

            created = updated = 0
            errors = []
            for p in products:
                try:
                    with transaction.atomic():
                        code = p['code']
                        rubro = p['rubro'] or SIN_RUBRO
                        cat = cat_map.get(rubro) or cat_map.get(rubro.upper()) or cat_map.get(SIN_RUBRO)
                        brand = cat_map.get(('brand', p['marca'])) if p['marca'] else None

                        prod = None
                        # match por código (excepto códigos duplicados en el origen,
                        # que apuntan a productos distintos -> usar nombre)
                        if code and code not in dup_codes:
                            prod = Product.objects.filter(code=code).first()
                        if prod is None:
                            prod = Product.objects.filter(name=p['name']).first()
                        if prod is None:
                            prod = Product(code=code, name=p['name'], cat=cat, company=company)
                            created += 1
                        else:
                            prod.name = p['name']
                            if cat:
                                prod.cat = cat
                            if company:
                                prod.company = company
                            updated += 1

                        prod.external_code = p['external_code']
                        prod.brand = brand
                        prod.supplier = sup_map.get(p['proveedor'])
                        prod.cost_price = p['cost_price'] or Decimal('0')
                        prod.pvp = p['pvp'] or Decimal('0')
                        if p['pvp_final'] is not None:
                            prod.pvp_final = p['pvp_final']
                        prod.iva_rate = p['iva_rate']
                        prod.margin_percentage = p['margin_percentage']
                        prod.stock = p['stock']
                        prod.synced_to_server = False
                        prod.save()
                except IntegrityError as e:
                    errors.append(f'{p["name"][:40]}: {e}')
                except Exception as e:
                    errors.append(f'{p["name"][:40]}: {e}')
            self.stdout.write(f'  Productos creados: {created} | actualizados: {updated} | errores: {len(errors)}')
            for e in errors[:10]:
                self.stdout.write(f'    {e}')

        self.stdout.write('')
        self.stdout.write('Importación finalizada.')
