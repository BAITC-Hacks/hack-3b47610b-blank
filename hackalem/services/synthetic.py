"""Offline fixture creation through the shared normalized contract and quality gate.

Only this preparation/evaluation service reads the oracle. Model inputs reside
in model/ and contain no latent demand, event labels or scenario registry.
"""

from collections import defaultdict
from contextlib import closing
from datetime import date
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from hackalem.config import PROJECT_ROOT, load_settings
from hackalem.domain.quality_config import months
from hackalem.services.datasets import dataset_context
from hackalem.services.quality import run_quality, save_configuration, quality_report
from hackalem.services.systeme import _code_manifest, _json, _profile
from hackalem.storage import initialize_database

DEFAULT_SEED = 20260923
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / '.local' / 'synthetic'
SHEET = 'Synthetic'
_MODEL_TABLES = (
    'import_files', 'import_sheets', 'source_rows', 'products', 'transactions',
    'monthly_values', 'measures', 'seasonal_values', 'incoming_orders',
    'import_issues', 'import_locations', 'catalog_items', 'external_sources',
    'row_metadata', 'unit_assessments',
    'snapshots', 'snapshot_files', 'synthetic_customers', 'synthetic_availability', 'synthetic_products',
)
_FILES = ('model/observations.json', 'validation/truth.json',
          'validation/scenarios.json', 'validation/spec.json')


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _encoded(value):
    return (_json(value) + '\n').encode('utf-8')


def _model_fingerprint(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        digest = hashlib.sha256()
        for row in connection.execute("SELECT key,value FROM app_metadata WHERE key IN ('dataset_kind','dataset_id','synthetic_seed','synthetic_as_of') ORDER BY key"):
            digest.update(_encoded(row))
        for table in _MODEL_TABLES:
            digest.update(table.encode())
            for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid'):
                digest.update(_encoded(row))
        return digest.hexdigest()


def _entry(value):
    return {'value': value, 'status': 'scenario', 'reason': 'Синтетическое условие проверочного набора; не бизнес-подтверждение.', 'author': 'synthetic-generator'}


class _SourceWriter:
    def __init__(self, connection, file_id):
        self.connection, self.file_id = connection, file_id
        self.row = 0
        connection.execute('INSERT INTO import_sheets VALUES (?, ?, ?, ?, ?, ?)',
                           (file_id, SHEET, 'visible', None, 0, 12))

    def raw(self, cells):
        self.row += 1
        values = {column: {'value': value, 'formula': None, 'data_type': 'n' if isinstance(value, (int, float)) else 's', 'number_format': 'General'} for column, value in cells.items()}
        self.connection.execute('INSERT INTO source_rows VALUES (?, ?, ?, ?)',
                                (self.file_id, SHEET, self.row, _json(values)))
        return self.row

    def fact(self, table, row, **values):
        values = {'file_id': self.file_id, 'sheet': SHEET, 'row': row, **values}
        columns = ','.join(values)
        marks = ','.join('?' for _ in values)
        self.connection.execute(f'INSERT INTO {table} ({columns}) VALUES ({marks})', tuple(values.values()))

    def finish(self):
        self.connection.execute('UPDATE import_sheets SET max_row=?, declared_dimension=? WHERE file_id=? AND sheet=?',
                                (self.row, f'A1:L{max(1, self.row)}', self.file_id, SHEET))


def _populate(database, bundle):
    """Write only the observed partition; never traverse bundle['truth']."""
    initialize_database(database)
    manifest, observed = bundle['manifest'], bundle['observed']
    fixed_time = manifest['as_of'] + 'T00:00:00+00:00'
    code_version, code_manifest = _code_manifest()
    snapshots, configurations = [], {}
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.executemany('INSERT INTO app_metadata VALUES (?, ?)', [
            ('dataset_kind', 'synthetic'), ('dataset_id', manifest['dataset_id']),
            ('synthetic_as_of', manifest['as_of']), ('synthetic_seed', str(manifest['seed'])),
        ])
        connection.execute('''CREATE TABLE synthetic_customers (
            file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
            customer_id TEXT NOT NULL, PRIMARY KEY(file_id,sheet,row),
            FOREIGN KEY(file_id,sheet,row) REFERENCES transactions(file_id,sheet,row))''')
        connection.execute('''CREATE TABLE synthetic_products (
            sku TEXT PRIMARY KEY, launch_date TEXT NOT NULL)''')
        connection.execute('''CREATE TABLE synthetic_availability (
            sku TEXT NOT NULL REFERENCES synthetic_products(sku), date TEXT NOT NULL,
            available INTEGER CHECK(available IN (0,1)), observed_hours REAL,
            PRIMARY KEY(sku,date))''')
        connection.executemany('INSERT INTO synthetic_products VALUES (?, ?)',
                               ((product['sku'], product['launch_date']) for product in observed['products']))
        connection.executemany('INSERT INTO synthetic_availability VALUES (?, ?, ?, ?)',
                               ((row['sku'], row['date'], row['available'], row['observed_hours']) for row in observed['availability']))
        monthly = defaultdict(list)
        for transaction in observed['transactions']:
            monthly[transaction['sku'], transaction['date'][:7] + '-01'].append(transaction)
        for supplier in sorted({product['supplier'] for product in observed['products']}):
            products = [p for p in observed['products'] if p['supplier'] == supplier]
            skus = {p['sku'] for p in products}
            transactions = [t for t in observed['transactions'] if t['sku'] in skus]
            arrivals = [item for item in observed['incoming'] if item['sku'] in skus]
            parameters = {sku: {key: _entry(value) for key, value in observed['parameters'][sku].items()} for sku in skus}
            members = []
            for source in sorted(_profile(supplier)[2]):
                source_name = f'SYNTHETIC-{supplier}-{source}.json'
                source_hash = _digest(_encoded({'source_kind': source, 'supplier': supplier,
                                              'observed': observed, 'version': manifest['generator_version']}))
                cursor = connection.execute('''INSERT INTO import_files
                    (source_kind,supplier,path,source_name,sha256,imported_at_utc,snapshot_date,rules_version,parser_version)
                    VALUES (?,?,?,?,?,?,?,?,?)''', (source, supplier,
                    f"synthetic://{manifest['dataset_id']}/{source_name}", source_name,
                    source_hash, fixed_time, manifest['as_of'], 'synthetic-contract-1', manifest['generator_version']))
                file_id = cursor.lastrowid
                members.append({'source_kind': source, 'file_id': file_id})
                writer = _SourceWriter(connection, file_id)
                product_rows = {}
                if source != 'seasonality':
                    for product in products:
                        row = writer.raw({'A': product['sku'], 'B': product['name'], 'J': product['unit'], 'K': product['article'], 'L': product['launch_date']})
                        product_rows[product['sku']] = row
                        writer.fact('products', row, sku=product['sku'], name=product['name'], article=product['article'], unit=product['unit'], cell=f'A{row}')
                if source == 'transactions':
                    for item in transactions:
                        row = writer.raw({'A': item['sku'], 'B': item['date'], 'C': item['quantity'], 'D': item['document_number'], 'E': item['document_type'], 'F': item['customer_id'], 'G': item['warehouse'], 'H': item['unit']})
                        writer.fact('transactions', row, sku=item['sku'], occurred_at=item['date'], document_number=item['document_number'], document_type=item['document_type'], unit=item['unit'], warehouse=item['warehouse'], quantity=item['quantity'], state=item['state'], cell=f'C{row}')
                        if item['customer_id'] is not None:
                            connection.execute('INSERT INTO synthetic_customers VALUES (?, ?, ?, ?)', (file_id, SHEET, row, item['customer_id']))
                if source in ('monthly_sales', 'current', 'monthly_stock'):
                    for product in products:
                        for period in months(manifest['start'], manifest['end'][:7] + '-01'):
                            items = monthly.get((product['sku'], period), [])
                            # Before launch there is no fabricated sales history.
                            if not items and source != 'monthly_stock':
                                continue
                            value = sum(item['quantity'] for item in items) if items and all(item['state'] == 'value' for item in items) else None
                            if source == 'monthly_stock':
                                value = None  # A daily availability flag is not a measured stock quantity.
                            row = writer.raw({'A': product['sku'], 'B': period, 'C': value})
                            series = ('opening_stock' if supplier == 'IEK' else 'stock') if source == 'monthly_stock' else 'sales'
                            writer.fact('monthly_values', row, sku=product['sku'], period=period, series=series, quantity=value, state='value' if value is not None else 'blank', cell=f'C{row}')
                if source in ('minimums', 'multiples', 'current'):
                    for product in products:
                        sku = product['sku']
                        values = observed['parameters'][sku]
                        metrics = [('minimum_order', values.get('minimum_order')), ('order_multiple', values.get('order_multiple'))] if source in ('minimums', 'multiples') else [('stock', values.get('current_stock')), ('reserved_stock', values.get('reserved_stock')), ('free_stock', values['current_stock'] - values['reserved_stock'] if 'current_stock' in values and 'reserved_stock' in values else None)]
                        for metric, value in metrics:
                            row = writer.raw({'A': sku, 'B': metric, 'C': value})
                            writer.fact('measures', row, sku=sku, metric=metric, number=value, text=None, state='value' if value is not None else 'blank', cell=f'C{row}')
                if source in ('current', 'incoming'):
                    for arrival in arrivals:
                        row = writer.raw({'A': arrival['sku'], 'B': arrival['order_number'], 'C': arrival['quantity'], 'D': arrival['eta'], 'E': arrival['order_date'], 'F': arrival['unit']})
                        if source == 'current':
                            writer.fact('measures', row, sku=arrival['sku'], metric='incoming_quantity', number=arrival['quantity'], text=None, state='value', cell=f'C{row}')
                        else:
                            writer.fact('incoming_orders', row, sku=arrival['sku'], article=None, order_number=arrival['order_number'], order_date=arrival['order_date'], eta_deadline=arrival['eta'], quantity=arrival['quantity'], state='value', unit=arrival['unit'], cell=f'C{row}', header_cell=f'D{row}')
                        parameter = parameters[arrival['sku']].setdefault('eta_confirmations', _entry([]))
                        parameter['value'].append({'source_kind': source, 'sheet': SHEET, 'cell': f'C{row}', 'eta': arrival['eta'], 'meaning': 'expected'})
                if source == 'seasonality':
                    # Empty aggregate source: future models estimate seasonality from history.
                    writer.raw({'A': 'SYNTHETIC: сезонные коэффициенты не заданы; используйте историю.'})
                writer.finish()
            snapshot_parameters = {'dataset': 'synthetic', 'dataset_id': manifest['dataset_id'], 'seed': manifest['seed'], 'generator_version': manifest['generator_version'], 'as_of': manifest['as_of']}
            fingerprint = _digest(_encoded({'members': members, 'parameters': snapshot_parameters, 'supplier': supplier, 'code': code_version}))
            cursor = connection.execute('''INSERT INTO snapshots
                (fingerprint,created_at_utc,code_version,code_manifest_json,parameter_version,parameters_json,supplier)
                VALUES (?,?,?,?,?,?,?)''', (fingerprint, fixed_time, code_version, _json(code_manifest), _digest(_encoded(snapshot_parameters)), _json(snapshot_parameters), supplier))
            snapshot_id = cursor.lastrowid
            connection.executemany('INSERT INTO snapshot_files VALUES (?, ?, ?)', ((snapshot_id, item['source_kind'], item['file_id']) for item in members))
            choices = [{'sku': product['sku'], 'start': max(manifest['start'], product['launch_date'][:7] + '-01'), 'end': manifest['end'][:7] + '-01', 'source': 'transactions', 'scope': 'source_report', 'status': 'scenario', 'reason': 'Синтетический журнал продаж с явными пустотами и нулями.', 'author': 'synthetic-generator'} for product in products]
            configurations[snapshot_id] = {'as_of': manifest['as_of'], 'defaults': {}, 'skus': parameters, 'sales_choices': choices}
            snapshots.append({'supplier': supplier, 'snapshot_id': snapshot_id})
    for record in snapshots:
        config = save_configuration(database, record['snapshot_id'], configurations[record['snapshot_id']])
        report = run_quality(database, record['snapshot_id'], config['id'])
        record.update(configuration_id=config['id'], run_id=report['run_id'])
    return snapshots


def _validate_existing(dataset_dir):
    dataset_dir = Path(dataset_dir).resolve()
    path = dataset_dir / 'manifest.json'
    if not path.is_file():
        raise ValueError('Папка уже существует и не является проверочным dataset; перезапись запрещена.')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('dataset_kind') != 'synthetic' or set(manifest.get('files', {})) != set(_FILES):
        raise ValueError('Неверный манифест синтетического dataset.')
    for relative, digest in manifest['files'].items():
        if _digest((dataset_dir / relative).read_bytes()) != digest:
            raise ValueError(f'Повреждён проверочный dataset: {relative}. Перезапись запрещена.')
    database = dataset_dir / 'model/hackalem.sqlite3'
    context = dataset_context(database)
    if context['kind'] != 'synthetic' or context['dataset_id'] != manifest['dataset_id']:
        raise ValueError('Тип или идентификатор базы не соответствует манифесту.')
    if _model_fingerprint(database) != manifest['model_fingerprint']:
        raise ValueError('Нормализованные входы dataset изменены. Перезапись запрещена.')
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Нарушена целостность базы dataset.')
    return manifest


def create_synthetic_dataset(output_root=DEFAULT_OUTPUT_ROOT, seed=DEFAULT_SEED):
    from hackalem.synthetic.generator import generate_dataset
    bundle = generate_dataset(seed)
    # Keep the caller-visible path spelling (notably /var vs /private/var on macOS)
    # while resolving only comparisons against protected source directories.
    root = Path(output_root).absolute()
    resolved_root = root.resolve()
    for protected_root in (PROJECT_ROOT, load_settings().source_dir):
        for folder in ('IEK', 'Systeme electric'):
            if resolved_root.is_relative_to((protected_root / folder).resolve()):
                raise ValueError('Нельзя создавать dataset в папке исходных файлов.')
    destination = root / bundle['manifest']['dataset_id']
    # Deterministic, explicit artifacts. Runtime timestamps are only in SQLite results.
    spec = json.loads((PROJECT_ROOT / 'hackalem/synthetic/validation_spec.json').read_text(encoding='utf-8'))
    contents = dict(zip(_FILES, map(_encoded, (bundle['observed'], bundle['truth'], bundle['scenarios'], spec))))
    digests = {relative: _digest(content) for relative, content in contents.items()}
    if destination.exists():
        manifest = _validate_existing(destination)
        if manifest['files'] != digests:
            raise ValueError('Этот dataset создан другой версией генератора/эталона. Выберите другую выходную папку; старый сохранён.')
        return synthetic_report(destination)
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.synthetic-build-', dir=root) as temporary:
        staging = Path(temporary) / 'dataset'
        staging.mkdir()
        for relative, content in contents.items():
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
        snapshots = _populate(staging / 'model/hackalem.sqlite3', bundle)
        manifest = {**bundle['manifest'], 'files': digests, 'model_fingerprint': _model_fingerprint(staging / 'model/hackalem.sqlite3'), 'snapshots': snapshots}
        (staging / 'manifest.json').write_bytes(_encoded(manifest))
        _validate_existing(staging)
        if destination.exists():
            raise ValueError('Dataset создан другим процессом; существующая папка сохранена.')
        staging.rename(destination)
    return synthetic_report(destination)


def synthetic_report(dataset_dir):
    directory = Path(dataset_dir).absolute()
    manifest = _validate_existing(directory)
    database = directory / 'model/hackalem.sqlite3'
    spec = json.loads((directory / 'validation/spec.json').read_text(encoding='utf-8'))
    scenarios = json.loads((directory / 'validation/scenarios.json').read_text(encoding='utf-8'))
    snapshots = []
    for record in manifest['snapshots']:
        report = quality_report(database, record['run_id'])
        snapshots.append({**record, 'sku_count': report['summary']['sku_count'], 'status_counts': report['summary']['status_counts']})
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        counts = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('transactions', 'synthetic_customers', 'synthetic_availability', 'synthetic_products')}
    return {'dataset_id': manifest['dataset_id'], 'dataset_dir': str(directory), 'database_path': str(database),
            'manifest': manifest, 'snapshots': snapshots, 'counts': counts,
            'validation': {'integrity': 'ok', 'artifact_hashes': 'ok', 'model_fingerprint': 'ok', 'scenario_count': len(scenarios), 'requirements': spec['requirements'], 'manual_cases': spec['manual_cases'], 'note': 'Готовность набора проверена. Качество будущих прогнозов и заказов ещё не проверено.'}}


def evaluate_forecasts(dataset_dir, run_ids):
    """Evaluator-only truth comparison; forecast services never call this function."""
    directory = Path(dataset_dir).resolve()
    _validate_existing(directory)
    if not isinstance(run_ids, list) or not run_ids or any(isinstance(value, bool) or not isinstance(value, int) for value in run_ids):
        raise ValueError('Укажите непустой список номеров прогнозов.')
    database = directory / 'model/hackalem.sqlite3'
    scenarios = json.loads((directory / 'validation/scenarios.json').read_text(encoding='utf-8'))
    truth = json.loads((directory / 'validation/truth.json').read_text(encoding='utf-8'))
    spec = json.loads((directory / 'validation/spec.json').read_text(encoding='utf-8'))
    by_sku = {item['sku']: item for item in scenarios}
    monthly = defaultdict(float)
    for row in truth['daily']:
        if row['regular_demand'] is not None:
            monthly[row['sku'], row['date'][:7] + '-01'] += row['regular_demand']
    from hackalem.services.forecasting import forecast_report
    reports = []
    for run_id in run_ids:
        report = forecast_report(database, run_id)
        scenario = by_sku.get(report['sku'])
        if scenario is None:
            raise ValueError('Прогноз не относится к SKU этого синтетического эталона.')
        points = report['summary']['backtest']
        compared = [{**point, 'truth': monthly[report['sku'], point['period']]}
                    for point in points if (report['sku'], point['period']) in monthly]
        denominator = sum(point['truth'] for point in compared)
        errors = [point['prediction'] - point['truth'] for point in compared]
        predictions = [point['prediction'] for point in compared]
        actual = [point['truth'] for point in compared]
        metrics = {
            'count': len(compared),
            'wape': sum(abs(value) for value in errors) / denominator if denominator else None,
            'mae_units': sum(abs(value) for value in errors) / len(errors) if errors else None,
            'bias_units': sum(errors) / len(errors) if errors else None,
            'finite_nonnegative_fraction': (sum(isfinite(value) and value >= 0 for value in predictions) / len(predictions)
                                            if predictions else None),
        }
        if scenario['id'] == 'seasonal' and predictions:
            predicted_mean, actual_mean = sum(predictions) / len(predictions), sum(actual) / len(actual)
            predicted_peak, actual_peak = max(predictions), max(actual)
            metrics.update(
                peak_to_mean_relative_error=abs(predicted_peak / predicted_mean - actual_peak / actual_mean) / (actual_peak / actual_mean),
                predicted_peak_month=compared[predictions.index(predicted_peak)]['period'],
                actual_peak_month=compared[actual.index(actual_peak)]['period'],
            )
        if scenario['id'] == 'growth' and len(predictions) == 12:
            predicted_ratio = (sum(predictions[9:12]) / 3) / (sum(predictions[:3]) / 3)
            actual_ratio = (sum(actual[9:12]) / 3) / (sum(actual[:3]) / 3)
            metrics.update(growth_ratio=predicted_ratio,
                           growth_ratio_relative_error=abs(predicted_ratio - actual_ratio) / actual_ratio,
                           last_quarter_gt_first=predicted_ratio > 1)
        reports.append({'run_id': run_id, 'scenario': scenario['id'], 'sku': report['sku'],
                        'selected_model': report['selected_model'], 'metrics': metrics,
                        'model_selection': report['summary']['model_selection'],
                        'limitations': report['summary']['limitations']})
    primary = {item['scenario']: item for item in reports if item['scenario'] in ('stable', 'seasonal', 'growth')}
    acceptance = next(item['acceptance'] for item in spec['requirements'] if item['id'] == 'seasonality_growth')
    checks = {}
    for scenario in ('stable', 'seasonal', 'growth'):
        item = primary.get(scenario)
        checks[f'{scenario}_present'] = item is not None
        if item:
            checks[f'{scenario}_wape'] = item['metrics']['wape'] <= acceptance[f'{scenario}_wape_max']
            difference = item['model_selection'].get('wape_difference_to_best_baseline')
            checks[f'{scenario}_baseline_difference'] = difference is not None and difference <= acceptance['wape_difference_to_best_baseline_max']
    if 'seasonal' in primary:
        checks['seasonal_peak_shape'] = primary['seasonal']['metrics'].get('peak_to_mean_relative_error', float('inf')) <= acceptance['seasonal_peak_to_mean_relative_error_max']
    if 'growth' in primary:
        checks['growth_ratio'] = primary['growth']['metrics'].get('growth_ratio_relative_error', float('inf')) <= acceptance['growth_ratio_relative_error_max']
        checks['growth_direction'] = primary['growth']['metrics'].get('last_quarter_gt_first') is True
    return {'dataset_id': dataset_context(database)['dataset_id'], 'evaluator_only_truth_used': True,
            'forecast_model_oracle_access': False, 'reports': reports, 'checks': checks,
            'passed': bool(checks) and all(checks.values()),
            'note': 'Синтетические метрики не доказывают точность на реальных цензурированных продажах.'}


def read_observed_context(database_path, snapshot_id, cutoff):
    """Public synthetic client/availability context at a cutoff, with no oracle IO."""
    if dataset_context(database_path)['kind'] != 'synthetic':
        raise ValueError('Синтетические клиенты и журнал наличия недоступны для реальных товаров.')
    parsed = date.fromisoformat(cutoff)
    if parsed.isoformat() != cutoff:
        raise ValueError('Нужна дата YYYY-MM-DD.')
    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        snapshot = connection.execute('SELECT parameters_json FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if snapshot is None:
            raise ValueError('Снимок не найден.')
        as_of = json.loads(snapshot['parameters_json'])['as_of']
        if cutoff >= as_of:
            raise ValueError('Журнал доступен только до даты среза, без будущих наблюдений.')
        customers = [dict(row) for row in connection.execute('''SELECT t.sku,t.occurred_at,c.customer_id,t.document_number,t.quantity,t.state,t.file_id,t.sheet,t.row,t.cell
            FROM transactions t JOIN synthetic_customers c ON c.file_id=t.file_id AND c.sheet=t.sheet AND c.row=t.row
            JOIN snapshot_files s ON s.file_id=t.file_id WHERE s.snapshot_id=? AND substr(t.occurred_at,1,10)<=? ORDER BY t.occurred_at,t.row''', (snapshot_id, cutoff))]
        availability = [dict(row) for row in connection.execute('''SELECT a.* FROM synthetic_availability a
            WHERE a.date<=? AND a.sku IN (SELECT p.sku FROM products p JOIN snapshot_files s ON s.file_id=p.file_id WHERE s.snapshot_id=?) ORDER BY a.sku,a.date''', (cutoff, snapshot_id))]
        return {'dataset': dataset_context(database_path), 'cutoff': cutoff, 'customers': customers, 'availability': availability}
