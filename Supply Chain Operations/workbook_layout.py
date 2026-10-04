"""Targeted column-width repairs, without rebuilding the large cleaned tables."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET


def refine(path, dimensions=None):
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    repairs = {'financial_checks': {1: 62}, 'field_disposition': {3: 82}, 'Validation Checks': {1: 76},
               'comparable_market_summary': {2: 62}, 'comparable_market_monthly': {2: 62}}
    with ZipFile(path) as source:
        book = ET.fromstring(source.read('xl/workbook.xml'))
        sheet_names = [element.attrib['name'] for element in book.findall('m:sheets/m:sheet', ns)]
        changed = {}
        for name, widths in repairs.items():
            member = f'xl/worksheets/sheet{sheet_names.index(name)+1}.xml'
            data = source.read(member).decode('utf-8')
            # Preserve all XML namespaces and styles; alter only existing column width attributes.
            import re
            for column, width in widths.items():
                pattern = r'<col\b[^>]*/>'
                def replace(match):
                    tag = match.group(0)
                    node = ET.fromstring(tag)
                    if int(node.attrib['min']) <= column <= int(node.attrib['max']):
                        return re.sub(r'width="[^"]+"', f'width="{width}"', tag)
                    return tag
                data = re.sub(pattern, replace, data)
            changed[member] = data.encode('utf-8')
        # Keep the central late-rate metric auditable in Excel, with cached values
        # matching the same validated Python calculations used by other artifacts.
        import re
        from openpyxl.utils import get_column_letter
        for index, name in enumerate(sheet_names, 1):
            if name in {'Dataset Guide', 'Order Level', 'Item Level', 'Data Quality'}:
                continue
            member = f'xl/worksheets/sheet{index}.xml'
            data = changed.get(member, source.read(member)).decode('utf-8')
            tree = ET.fromstring(data)
            first = tree.find('m:sheetData/m:row', ns)
            headers = {}
            for cell in first:
                text = cell.find('m:is/m:t', ns)
                if text is not None:
                    headers[text.text] = re.sub(r'\d+', '', cell.attrib['r'])
            late = headers.get('late_orders', headers.get('sum'))
            eligible = headers.get('eligible_orders', headers.get('count'))
            if 'late_rate' not in headers or late is None or eligible is None:
                continue
            column = headers['late_rate']
            def rate_formula(match):
                row = int(match.group(2))
                return match.group(1) + f'<f>IFERROR({late}{row}/{eligible}{row},0)</f>' + match.group(3)
            data = re.sub(r'(<c\b[^>]*\br="' + column + r'(\d+)"[^>]*>)(<v>[^<]+</v></c>)', rate_formula, data)
            changed[member] = data.encode('utf-8')
        temporary = Path(path).with_suffix('.layout.tmp')
        sheet_dimensions = {f'xl/worksheets/sheet{index}.xml': dimensions[name] for index, name in enumerate(sheet_names, 1)} if dimensions else {}
        with ZipFile(temporary, 'w', ZIP_DEFLATED) as destination:
            for member in source.infolist():
                content = changed.get(member.filename, source.read(member.filename))
                if member.filename in sheet_dimensions and b'<dimension ' not in content:
                    dimension = sheet_dimensions[member.filename]
                    content = content.replace(b'<sheetViews', f'<dimension ref="{dimension}"/><sheetViews'.encode('utf-8'), 1)
                elif member.filename.startswith('xl/worksheets/sheet') and b'<dimension ' not in content:
                    # Streaming writers omit dimensions; restore them for reliable readers.
                    row_number = 1
                    for match in re.finditer(rb'<row\b[^>]*\br="(\d+)"', content):
                        row_number = int(match.group(1))
                    first_row = content.split(b'</row>', 1)[0]
                    columns = re.findall(rb'<c\b[^>]*\br="([A-Z]+)1"', first_row)
                    if columns:
                        dimension = f'A1:{columns[-1].decode("ascii")}{row_number}'
                        content = content.replace(b'<sheetViews', f'<dimension ref="{dimension}"/><sheetViews'.encode('utf-8'), 1)
                destination.writestr(member, content)
    temporary.replace(path)


if __name__ == '__main__':
    refine(Path(__file__).resolve().parent / 'outputs/DataCo_Operations_Analytics_Review.xlsx')
