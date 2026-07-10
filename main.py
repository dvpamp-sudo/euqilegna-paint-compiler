from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pathlib import Path
import tempfile, zipfile
from compiler import compile_artwork

app = FastAPI(title='Euqilegna Paint Compiler Phase 1')

@app.get('/', response_class=HTMLResponse)
def home():
    return '''<h1>Euqilegna Paint Compiler</h1><form action="/compile" method="post" enctype="multipart/form-data"><input type="file" name="file" required><p>Colors <input name="colors" value="24"></p><p>Detail <select name="detail"><option>simple</option><option selected>balanced</option><option>detailed</option></select></p><p>Minimum region area <input name="min_region_area" value="220"></p><p>Merge strength <input name="merge_strength" value="28"></p><p>Outline width <input name="outline_width" value="0.5"></p><button>Compile</button></form>'''

@app.get('/health')
def health():
    return {'ok': True}

@app.post('/compile')
async def compile_endpoint(file: UploadFile = File(...), colors: int = Form(24), detail: str = Form('balanced'), min_region_area: int = Form(220), merge_strength: float = Form(28), outline_width: float = Form(0.5)):
    work = Path(tempfile.mkdtemp())
    try:
        inp = work / file.filename
        inp.write_bytes(await file.read())
        out = work / 'package'; out.mkdir()
        compile_artwork(inp, out, colors, detail, min_region_area, merge_strength, outline_width)
        zpath = work / 'euqilegna_paint_package.zip'
        with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
            for item in out.iterdir(): z.write(item, item.name)
        return FileResponse(zpath, filename='euqilegna_paint_package.zip', media_type='application/zip')
    except Exception as e:
        return JSONResponse(status_code=500, content={'error': str(e)})
