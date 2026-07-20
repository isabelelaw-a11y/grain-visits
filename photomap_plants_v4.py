"""
Generates photomap_plants_v4.html.

Same as v3 (square thumbnails, POWO native range popup, Wikipedia hover,
non-native amber dotted outline) PLUS:

  - Fetches photos uploaded via grain_upload.html from a GitHub repo at
    page load and adds them as new monthly layers.
  - Configure GITHUB_OWNER + GITHUB_REPO below before running.
"""

import base64
import csv
import io
import json
import time
from pathlib import Path

import folium
import requests
from PIL import Image
from shapely.geometry import shape

# ── config ────────────────────────────────────────────────────────────────────

ROOT     = Path(__file__).parent
CSV_PATH = ROOT / "plant_catalogue.csv"
THUMB_PX = 55
JPEG_Q   = 55

# Set these to your GitHub username and repo name.
# The map will fetch photos/metadata committed by grain_upload.html.
GITHUB_OWNER = "isabelelaw-a11y"
GITHUB_REPO  = "grain-visits"

FOLDER_COLOURS = {
    "1":                 "#FFACDE",
    "2 (With Dad)":      "#AAFF55",
    "3 (With Jonathan)": "#FF33CC",
    "4 (with Tom)":      "#1A7050",
}

POWO_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}
WIKI_HEADERS = {
    "User-Agent": "GrainVisitPhotomap/1.0 (botanical mapping project)",
    "Accept": "application/json",
}

# ── TDWG → Natural Earth country name mapping ─────────────────────────────────

TDWG_TO_NE = {
    "Great Britain":          ["United Kingdom"],
    "Ireland":                ["Ireland"],
    "Baleares":               ["Spain"],
    "Canary Is.":             ["Spain"],
    "Corse":                  ["France"],
    "France":                 ["France"],
    "Sardegna":               ["Italy"],
    "Sicilia":                ["Italy"],
    "Kriti":                  ["Greece"],
    "East Aegean Is.":        ["Greece"],
    "Türkiye":                ["Turkey"],
    "Türkiye-in-Europe":      ["Turkey"],
    "Lebanon-Syria":          ["Lebanon", "Syria"],
    "Palestine":              ["Palestine"],
    "Sinai":                  ["Egypt"],
    "North Caucasus":         ["Russia"],
    "Transcaucasus":          ["Georgia", "Armenia", "Azerbaijan"],
    "NW. Balkan Pen.":        ["Albania", "Bosnia and Herzegovina", "Croatia",
                               "Montenegro", "Slovenia", "North Macedonia"],
    "Baltic States":          ["Estonia", "Latvia", "Lithuania"],
    "Macaronesia":            ["Portugal", "Spain"],
    "Azores":                 ["Portugal"],
    "Madeira":                ["Portugal"],
    "Cape Verde":             ["Cape Verde"],
    "Réunion":                ["France"],
    "Mauritius":              ["Mauritius"],
    "Socotra":                ["Yemen"],
    "Tadzhikistan":           ["Tajikistan"],
    "Trinidad-Tobago":        ["Trinidad and Tobago"],
    "Leeward Is.":            [],
    "Windward Is.":           [],
    "Puerto Rico":            ["Puerto Rico"],
    **{s: ["United States of America"] for s in [
        "Alabama","Alaska","Arizona","Arkansas","California","Colorado",
        "Connecticut","Delaware","Florida","Georgia","Hawaii","Idaho",
        "Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana",
        "Maine","Maryland","Massachusetts","Michigan","Minnesota",
        "Mississippi","Missouri","Montana","Nebraska","Nevada",
        "New Hampshire","New Jersey","New Mexico","New York",
        "North Carolina","North Dakota","Ohio","Oklahoma","Oregon",
        "Pennsylvania","Rhode I.","South Carolina","South Dakota",
        "Tennessee","Texas","Utah","Vermont","Virginia","Washington",
        "West Virginia","Wisconsin","Wyoming",
    ]},
    **{p: ["Canada"] for p in [
        "Alberta","British Columbia","Manitoba","New Brunswick",
        "Newfoundland","Nova Scotia","Ontario","Prince Edward I.",
        "Québec","Saskatchewan","Yukon",
    ]},
    **{a: ["Argentina"] for a in [
        "Argentina Northeast","Argentina Northwest","Argentina South",
        "Buenos Aires","Chaco","Jujuy","Mendoza","Patagonia",
    ]},
    **{b: ["Brazil"] for b in [
        "Brazil North","Brazil Northeast","Brazil South","Brazil Southeast",
        "Brazil West-Central","Amapá","Amazonas",
    ]},
    **{c: ["China"] for c in [
        "China North-Central","China South-Central","China Southeast",
        "China Southwest","Inner Mongolia","Manchuria","Qinghai",
        "Tibet","Xinjiang",
    ]},
    **{i: ["India"] for i in [
        "Assam","East Himalaya","India","West Himalaya",
        "Andaman Is.","Nicobar Is.",
    ]},
    **{au: ["Australia"] for au in [
        "New South Wales","Northern Territory","Queensland",
        "South Australia","Tasmania","Victoria","Western Australia",
    ]},
    **{m: ["Mexico"] for m in [
        "Mexico Gulf","Mexico Northeast","Mexico Northwest",
        "Mexico Southeast","Mexico Southwest","Mexico Central",
    ]},
}


def tdwg_names_to_ne(tdwg_names):
    out = set()
    for name in tdwg_names:
        if name in TDWG_TO_NE:
            out.update(TDWG_TO_NE[name])
        else:
            out.add(name)
    out.discard("")
    return sorted(out)


# ── helpers ───────────────────────────────────────────────────────────────────

def parse_opacity(conf):
    if not conf or conf.startswith("<5") or conf == "0%":
        return 0.01
    if conf.startswith("n/a"):
        return round(0.5 ** 2, 4)
    try:
        linear = max(0.0, min(1.0, float(conf.strip("%")) / 100))
        return max(0.01, round(linear ** 2, 4))
    except ValueError:
        return 0.01


def make_thumb(path, size=THUMB_PX):
    try:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        scale = size / min(w, h)
        img = img.resize((max(size, int(w * scale)), max(size, int(h * scale))),
                         Image.LANCZOS)
        left = (img.width  - size) // 2
        top  = (img.height - size) // 2
        img  = img.crop((left, top, left + size, top + size))
        buf  = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_Q)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


# ── POWO distribution fetch ───────────────────────────────────────────────────

def fetch_powo_distribution(scientific_name):
    try:
        r = requests.get(
            "https://powo.science.kew.org/api/2/search",
            params={"q": scientific_name},
            headers=POWO_HEADERS, timeout=12,
        )
        if r.status_code != 200 or not r.json().get("results"):
            return None
        fq_id = r.json()["results"][0]["fqId"]
        r2 = requests.get(
            f"https://powo.science.kew.org/api/2/taxon/{fq_id}",
            params={"fields": "distribution"},
            headers=POWO_HEADERS, timeout=12,
        )
        if r2.status_code != 200:
            return None
        natives = r2.json().get("distribution", {}).get("natives", [])
        return [n["name"] for n in natives]
    except Exception as exc:
        print(f"    POWO error [{scientific_name}]: {exc}")
        return None


# ── Wikipedia langlinks fetch ─────────────────────────────────────────────────

def fetch_wiki_langlinks(scientific_name):
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action":  "query",
                "titles":  scientific_name,
                "prop":    "langlinks",
                "lllimit": "500",
                "format":  "json",
                "origin":  "*",
            },
            headers=WIKI_HEADERS, timeout=12,
        )
        if resp.status_code != 200:
            return {}
        pages = resp.json().get("query", {}).get("pages", {})
        for page_id, page in pages.items():
            if page_id == "-1":
                return {}
            links = {ll["lang"]: ll["*"] for ll in page.get("langlinks", [])}
            if "en" not in links:
                links["en"] = scientific_name
            return links
        return {}
    except Exception as exc:
        print(f"    Wiki error [{scientific_name}]: {exc}")
        return {}


# ── load plants ───────────────────────────────────────────────────────────────

plants = []
with open(CSV_PATH, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        folder   = row["visit_folder"]
        filename = row["filename"]
        img_path = ROOT / f"{folder}-plants" / filename
        if not img_path.exists():
            continue
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (ValueError, KeyError):
            continue

        thumb = make_thumb(img_path)
        if not thumb:
            continue

        plants.append({
            "lat":        lat,
            "lon":        lon,
            "thumb":      thumb,
            "scientific": row.get("scientific_name", ""),
            "common":     row.get("common_name", ""),
            "opacity":    parse_opacity(row.get("confidence", "")),
            "color":      FOLDER_COLOURS.get(folder, "#888888"),
        })

print(f"Loaded {len(plants)} plants.")

# ── fetch POWO + Wikipedia data for all identified species ────────────────────

identified = sorted({
    p["scientific"] for p in plants
    if p["scientific"] not in ("", "unidentified")
})
print(f"\nFetching POWO + Wikipedia for {len(identified)} species "
      f"(~{len(identified)*1.3/60:.1f} min)...")

powo_cache = {}
wiki_cache = {}

for idx, sp in enumerate(identified, 1):
    print(f"  [{idx:>3}/{len(identified)}] {sp}", end="", flush=True)

    tdwg = fetch_powo_distribution(sp)
    if tdwg:
        ne = tdwg_names_to_ne(tdwg)
        powo_cache[sp] = {
            "countries": ne,
            "nativeUK":  "United Kingdom" in ne,
        }
        print(f"  ✓ POWO ({len(ne)} regions)", end="", flush=True)
    else:
        print("  – POWO n/a", end="", flush=True)
    time.sleep(0.45)

    links = fetch_wiki_langlinks(sp)
    if links:
        wiki_cache[sp] = links
        print(f"  ✓ Wiki ({len(links)} langs)")
    else:
        print("  – Wiki n/a")
    time.sleep(0.3)

for p in plants:
    sp = p["scientific"]
    if sp in powo_cache:
        p["nativeCountries"] = powo_cache[sp]["countries"]
        p["nativeUK"]        = powo_cache[sp]["nativeUK"]
        p["nonNative"]       = (not powo_cache[sp]["nativeUK"]
                                and len(powo_cache[sp]["countries"]) > 0)
        p["hasRange"]        = True
    else:
        p["nativeCountries"] = []
        p["nativeUK"]        = True
        p["nonNative"]       = False
        p["hasRange"]        = False
    p["wikiLangs"] = wiki_cache.get(sp, {})
    p["wikiUrl"]   = (f"https://en.wikipedia.org/wiki/"
                      f"{sp.replace(' ', '_')}" if sp else "")

print(f"\nPOWO data: {len(powo_cache)} species  |  "
      f"Wikipedia: {len(wiki_cache)} species")
non_native_count = sum(1 for p in plants if p["nonNative"])
print(f"Non-native to GB: {non_native_count} plant photos")

# ── world countries GeoJSON + centroids ───────────────────────────────────────

print("\nFetching world GeoJSON...")
_geo_r = requests.get(
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_110m_admin_0_countries.geojson",
    timeout=30,
)
_geo_data = _geo_r.json()
world_geojson = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": feat["geometry"],
            "properties": {
                "n": feat["properties"].get("ADMIN") or feat["properties"].get("NAME", "")
            },
        }
        for feat in _geo_data["features"]
    ],
}
world_geojson_str = json.dumps(world_geojson, separators=(",", ":"))

country_centroids = {}
for feat in world_geojson["features"]:
    name = feat["properties"]["n"]
    try:
        geom = shape(feat["geometry"])
        if geom.geom_type == "MultiPolygon":
            geom = max(geom.geoms, key=lambda g: g.area)
        c = geom.centroid
        country_centroids[name] = [round(c.y, 4), round(c.x, 4)]
    except Exception:
        pass

print(f"World GeoJSON: {len(world_geojson_str)//1024} KB, "
      f"{len(world_geojson['features'])} countries, "
      f"{len(country_centroids)} centroids")

# ── build base map ────────────────────────────────────────────────────────────

avg_lat = sum(p["lat"] for p in plants) / len(plants)
avg_lon = sum(p["lon"] for p in plants) / len(plants)

m = folium.Map(location=[avg_lat, avg_lon], zoom_start=17,
               tiles="CartoDB positron")
m.get_root().html.add_child(folium.Element(
    "<style>.leaflet-tile-pane{filter:grayscale(100%)}</style>"
))

# ── build JS payload ──────────────────────────────────────────────────────────

plants_json = json.dumps([{
    "lat":            p["lat"],
    "lon":            p["lon"],
    "thumb":          p["thumb"],
    "scientific":     p["scientific"],
    "common":         p["common"],
    "opacity":        p["opacity"],
    "color":          p["color"],
    "nativeCountries": p["nativeCountries"],
    "nonNative":      p["nonNative"],
    "hasRange":       p["hasRange"],
    "wikiLangs":      p["wikiLangs"],
    "wikiUrl":        p["wikiUrl"],
} for p in plants])

overlay_js = f"""
<script>
(function(){{

var PLANTS      = {plants_json};
var EXTRA       = [];          /* populated from GitHub metadata.json */
var WORLD_GEO   = {world_geojson_str};
var NS          = 'http://www.w3.org/2000/svg';
var PHOTO       = {THUMB_PX};
var GAP         = 6;
var DOT_R       = 3;
var CURVE       = 20;
var ITER        = 100;
var LW          = 130;
var LH          = 26;

var GITHUB_OWNER = {json.dumps(GITHUB_OWNER)};
var GITHUB_REPO  = {json.dumps(GITHUB_REPO)};

/* ── Colour palette (same as grain_upload.html) ── */
var PALETTE = [
  "#FFACDE","#AAFF55","#FF33CC","#1A7050",
  "#FF7043","#AB47BC","#00ACC1","#FFD54F",
  "#66BB6A","#EF5350","#42A5F5","#B0BEC5"
];
function visitColor(name) {{
  var h = 5381;
  for (var i = 0; i < name.length; i++)
    h = ((h << 5) + h) + name.charCodeAt(i);
  return PALETTE[Math.abs(h) % PALETTE.length];
}}

/* Primary Wikipedia language for each Natural Earth country */
var COUNTRY_LANG = {{
  "Afghanistan":"fa","Albania":"sq","Algeria":"ar","Angola":"pt",
  "Argentina":"es","Armenia":"hy","Australia":"en","Austria":"de",
  "Azerbaijan":"az","Bangladesh":"bn","Belarus":"be","Belgium":"nl",
  "Belize":"en","Benin":"fr","Bhutan":"dz","Bolivia":"es",
  "Bosnia and Herzegovina":"bs","Botswana":"en","Brazil":"pt",
  "Brunei":"ms","Bulgaria":"bg","Burkina Faso":"fr","Burundi":"rn",
  "Cambodia":"km","Cameroon":"fr","Canada":"en",
  "Central African Republic":"fr","Chad":"fr","Chile":"es","China":"zh",
  "Colombia":"es","Costa Rica":"es","Croatia":"hr","Cuba":"es",
  "Cyprus":"el","Czechia":"cs",
  "Democratic Republic of the Congo":"fr","Denmark":"da",
  "Djibouti":"fr","Dominican Republic":"es","East Timor":"pt",
  "Ecuador":"es","Egypt":"ar","El Salvador":"es",
  "Equatorial Guinea":"es","Eritrea":"ti","Estonia":"et",
  "Ethiopia":"am","Fiji":"en","Finland":"fi","France":"fr",
  "Gabon":"fr","Gambia":"en","Georgia":"ka","Germany":"de",
  "Ghana":"en","Greece":"el","Greenland":"da","Guatemala":"es",
  "Guinea":"fr","Guinea-Bissau":"pt","Guyana":"en","Haiti":"ht",
  "Honduras":"es","Hungary":"hu","Iceland":"is","India":"hi",
  "Indonesia":"id","Iran":"fa","Iraq":"ar","Ireland":"ga",
  "Israel":"he","Italy":"it","Ivory Coast":"fr","Jamaica":"en",
  "Japan":"ja","Jordan":"ar","Kazakhstan":"kk","Kenya":"sw",
  "Kosovo":"sq","Kuwait":"ar","Kyrgyzstan":"ky","Laos":"lo",
  "Latvia":"lv","Lebanon":"ar","Lesotho":"st","Liberia":"en",
  "Libya":"ar","Lithuania":"lt","Luxembourg":"lb","Madagascar":"mg",
  "Malawi":"ny","Malaysia":"ms","Mali":"fr","Mauritania":"ar",
  "Mexico":"es","Moldova":"ro","Mongolia":"mn","Montenegro":"sr",
  "Morocco":"ar","Mozambique":"pt","Myanmar":"my","Namibia":"af",
  "Nepal":"ne","Netherlands":"nl","New Caledonia":"fr",
  "New Zealand":"en","Nicaragua":"es","Niger":"fr","Nigeria":"en",
  "North Korea":"ko","North Macedonia":"mk","Northern Cyprus":"tr",
  "Norway":"no","Oman":"ar","Pakistan":"ur","Palestine":"ar",
  "Panama":"es","Papua New Guinea":"en","Paraguay":"es","Peru":"es",
  "Philippines":"tl","Poland":"pl","Portugal":"pt","Puerto Rico":"es",
  "Qatar":"ar","Republic of Serbia":"sr",
  "Republic of the Congo":"fr","Romania":"ro","Russia":"ru",
  "Rwanda":"rw","Saudi Arabia":"ar","Senegal":"fr",
  "Sierra Leone":"en","Slovakia":"sk","Slovenia":"sl",
  "Solomon Islands":"en","Somalia":"so","Somaliland":"so",
  "South Africa":"af","South Korea":"ko","South Sudan":"en",
  "Spain":"es","Sri Lanka":"si","Sudan":"ar","Suriname":"nl",
  "Sweden":"sv","Switzerland":"de","Syria":"ar","Taiwan":"zh",
  "Tajikistan":"tg","Thailand":"th","The Bahamas":"en","Togo":"fr",
  "Trinidad and Tobago":"en","Tunisia":"ar","Turkey":"tr",
  "Turkmenistan":"tk","Uganda":"en","Ukraine":"uk",
  "United Arab Emirates":"ar","United Kingdom":"en",
  "United Republic of Tanzania":"sw",
  "United States of America":"en","Uruguay":"es","Uzbekistan":"uz",
  "Vanuatu":"bi","Venezuela":"es","Vietnam":"vi",
  "Western Sahara":"ar","Yemen":"ar","Zambia":"en","Zimbabwe":"en",
  "eSwatini":"ss"
}};

/* ── SVG helper ── */
function el(tag,attr){{
  var e=document.createElementNS(NS,tag);
  for(var k in attr) e.setAttribute(k,attr[k]);
  return e;
}}

function qPath(x1,y1,x2,y2,str){{
  var mx=(x1+x2)/2,my=(y1+y2)/2,dx=x2-x1,dy=y2-y1,len=Math.hypot(dx,dy)||1;
  return 'M'+x1+' '+y1+' Q'+(mx-dy/len*str)+' '+(my+dx/len*str)+' '+x2+' '+y2;
}}

function nearCorner(px,py,tx,ty){{
  var h=PHOTO/2,cs=[[px-h,py-h],[px+h,py-h],[px-h,py+h],[px+h,py+h]];
  return cs.reduce(function(b,c){{
    var d=Math.hypot(c[0]-tx,c[1]-ty);
    return d<b.d?{{d:d,c:c}}:b;
  }},{{d:Infinity,c:cs[0]}}).c;
}}

/* ── Layout ── */
function layout(pos){{
  var cell=PHOTO+GAP;
  for(var it=0;it<ITER;it++){{
    var moved=false;
    var grid={{}};
    for(var i=0;i<pos.length;i++){{
      var gx=Math.floor(pos[i].px/cell),gy=Math.floor(pos[i].py/cell);
      for(var dx=-1;dx<=1;dx++) for(var dy=-1;dy<=1;dy++){{
        var key=(gx+dx)+'|'+(gy+dy);
        (grid[key]||(grid[key]=[])).push(i);
      }}
    }}
    for(var i=0;i<pos.length;i++){{
      var p=pos[i];
      var key=Math.floor(p.px/cell)+'|'+Math.floor(p.py/cell);
      var nb=grid[key]||[];
      for(var ni=0;ni<nb.length;ni++){{
        var j=nb[ni]; if(j<=i) continue;
        var q=pos[j],dx=q.px-p.px,dy=q.py-p.py;
        var ox=cell-Math.abs(dx),oy=cell-Math.abs(dy);
        if(ox>0&&oy>0){{
          moved=true;
          if(ox<oy){{ var push=ox/2+0.5; if(dx<0)push=-push; p.px-=push;q.px+=push; }}
          else {{ var push=oy/2+0.5; if(dy<0)push=-push; p.py-=push;q.py+=push; }}
        }}
      }}
    }}
    if(!moved) break;
  }}
}}

/* ── Label placement ── */
function placeLabel(pp,photos,labels){{
  var h=PHOTO/2;
  var cands=[
    [pp.px+h+4,pp.py-LH/2],[pp.px-h-LW-4,pp.py-LH/2],
    [pp.px-LW/2,pp.py-h-LH-4],[pp.px-LW/2,pp.py+h+4],
    [pp.px+h+4,pp.py-h-LH],[pp.px+h+4,pp.py+h-LH/2],
    [pp.px-h-LW-4,pp.py-h-LH],[pp.px-h-LW-4,pp.py+h-LH/2],
  ];
  function hits(cx,cy){{
    for(var i=0;i<photos.length;i++){{
      var p=photos[i];
      if(cx<p.px+h+2&&cx+LW>p.px-h-2&&cy<p.py+h+2&&cy+LH>p.py-h-2) return true;
    }}
    for(var i=0;i<labels.length;i++){{
      var l=labels[i];
      if(cx<l[0]+LW+2&&cx+LW>l[0]-2&&cy<l[1]+LH+2&&cy+LH>l[1]-2) return true;
    }}
    return false;
  }}
  for(var i=0;i<cands.length;i++) if(!hits(cands[i][0],cands[i][1])) return cands[i];
  return cands[0];
}}

/* ── Get Leaflet map instance ── */
function getMap(){{
  for(var k in window){{
    try{{ var v=window[k]; if(v&&v._container&&typeof v.on==='function'&&v.getCenter) return v; }}
    catch(e){{}}
  }}
  return null;
}}

/* ═══════════════════════════════════════════════════════════
   WORLD MAP OVERLAY
   ═══════════════════════════════════════════════════════════ */

var worldOverlay=null, worldMap=null, nativeLayer=null;
var worldTitle=null, worldClose=null, worldWikiLink=null;
var wikiPanel=null, wikiCache={{}}, wikiHideTimer=null;

function buildOverlay(container){{
  worldOverlay=document.createElement('div');
  worldOverlay.style.cssText=
    'position:absolute;top:0;left:0;width:100%;height:100%;'
    +'background:rgba(255,255,255,0.93);z-index:490;display:none;';
  container.appendChild(worldOverlay);

  var mapDiv=document.createElement('div');
  mapDiv.id='world-map-div';
  mapDiv.style.cssText='width:100%;height:100%;';
  worldOverlay.appendChild(mapDiv);

  worldTitle=document.createElement('div');
  worldTitle.style.cssText=
    'position:fixed;top:16px;left:50%;transform:translateX(-50%);'
    +'font-family:"Times New Roman",serif;font-size:17px;font-style:italic;'
    +'z-index:10001;background:rgba(255,255,255,0.88);padding:3px 12px;'
    +'border-radius:4px;display:none;white-space:nowrap;color:#222;';
  document.body.appendChild(worldTitle);

  worldWikiLink=document.createElement('a');
  worldWikiLink.target='_blank';
  worldWikiLink.style.cssText=
    'position:fixed;top:44px;left:50%;transform:translateX(-50%);'
    +'font-family:sans-serif;font-size:11px;color:#0645ad;z-index:10001;'
    +'background:rgba(255,255,255,0.85);padding:1px 8px;border-radius:3px;'
    +'display:none;white-space:nowrap;text-decoration:none;';
  worldWikiLink.textContent='→ English Wikipedia';
  document.body.appendChild(worldWikiLink);

  var subLabel=document.createElement('div');
  subLabel.id='nr-sub';
  subLabel.style.cssText=
    'position:fixed;top:62px;left:50%;transform:translateX(-50%);'
    +'font-family:sans-serif;font-size:10px;color:#999;z-index:10001;'
    +'display:none;white-space:nowrap;';
  subLabel.textContent='Hover a highlighted country to see its Wikipedia article for this plant';
  document.body.appendChild(subLabel);

  worldClose=document.createElement('button');
  worldClose.textContent='✕  Close';
  worldClose.style.cssText=
    'position:fixed;top:14px;right:18px;z-index:10001;background:#f5f5f5;'
    +'border:1px solid #ccc;border-radius:4px;font-size:13px;padding:5px 10px;'
    +'cursor:pointer;font-family:sans-serif;display:none;';
  worldClose.onclick=hideOverlay;
  document.body.appendChild(worldClose);

  wikiPanel=document.createElement('div');
  wikiPanel.style.cssText=
    'position:fixed;z-index:20000;display:none;max-width:300px;'
    +'background:white;border:1px solid #ccc;border-radius:6px;'
    +'padding:10px;box-shadow:0 4px 16px rgba(0,0,0,0.2);'
    +'font-family:sans-serif;font-size:12px;line-height:1.45;pointer-events:none;';
  document.body.appendChild(wikiPanel);

  worldMap=L.map('world-map-div',{{
    zoomControl:true, attributionControl:false,
    minZoom:1, maxZoom:6,
  }}).setView([20,0],2);
  L.tileLayer(
    'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7',
    {{tileSize:256}}).addTo(worldMap);
}}

function hideOverlay(){{
  if(worldOverlay) worldOverlay.style.display='none';
  if(worldTitle)   worldTitle.style.display='none';
  if(worldClose)   worldClose.style.display='none';
  if(worldWikiLink) worldWikiLink.style.display='none';
  var s=document.getElementById('nr-sub'); if(s) s.style.display='none';
  if(wikiPanel) wikiPanel.style.display='none';
}}

function showNativeRange(plant){{
  worldOverlay.style.display='block';
  worldTitle.textContent=(plant.scientific||'Unknown')
    +(plant.common?' — '+plant.common:'');
  worldTitle.style.display='block';
  worldClose.style.display='block';
  if(plant.wikiUrl){{
    worldWikiLink.href=plant.wikiUrl;
    worldWikiLink.style.display='block';
  }}
  var s=document.getElementById('nr-sub'); if(s) s.style.display='block';

  if(nativeLayer){{ worldMap.removeLayer(nativeLayer); nativeLayer=null; }}
  var nativeSet={{}};
  (plant.nativeCountries||[]).forEach(function(c){{ nativeSet[c]=true; }});

  nativeLayer=L.geoJSON(WORLD_GEO,{{
    style:function(feature){{
      var isNative=!!nativeSet[feature.properties.n];
      return {{
        fillColor:   isNative?'#2d8a4e':'#d8d8d8',
        fillOpacity: isNative?0.50:0.25,
        color:'#aaa', weight:0.5,
      }};
    }},
    onEachFeature:function(feature,layer){{
      if(!nativeSet[feature.properties.n]) return;
      layer.on('mouseover',function(e){{ onCountryHover(e,plant,feature.properties.n); }});
      layer.on('mouseout', function(){{   onCountryOut(); }});
    }},
  }}).addTo(worldMap);

  setTimeout(function(){{ worldMap.invalidateSize(); }},80);
}}

/* ═══════════════════════════════════════════════════════════
   WIKIPEDIA HOVER
   ═══════════════════════════════════════════════════════════ */

function onCountryHover(e,plant,countryName){{
  clearTimeout(wikiHideTimer);
  var langCode=COUNTRY_LANG[countryName]||'en';
  var wikiLangs=plant.wikiLangs||{{}};
  var articleTitle=wikiLangs[langCode];
  if(!articleTitle && langCode!=='en'){{ langCode='en'; articleTitle=wikiLangs['en']; }}
  if(!articleTitle){{
    renderWikiPanel({{title:plant.scientific,extract:'No Wikipedia article found for this language.'}},e.originalEvent);
    return;
  }}
  var cacheKey=langCode+':'+encodeURIComponent(articleTitle);
  if(wikiCache[cacheKey]){{
    renderWikiPanel(wikiCache[cacheKey],e.originalEvent);
    return;
  }}
  var url='https://'+langCode+'.wikipedia.org/api/rest_v1/page/summary/'
    +encodeURIComponent(articleTitle.replace(/ /g,'_'));
  fetch(url,{{headers:{{'Accept':'application/json'}}}})
    .then(function(r){{ return r.ok?r.json():Promise.reject(r.status); }})
    .then(function(data){{
      wikiCache[cacheKey]=data;
      renderWikiPanel(data,e.originalEvent);
    }})
    .catch(function(){{
      renderWikiPanel({{title:articleTitle,extract:'Could not load article.'}},e.originalEvent);
    }});
}}

function onCountryOut(){{
  wikiHideTimer=setTimeout(function(){{
    if(wikiPanel) wikiPanel.style.display='none';
  }},300);
}}

function renderWikiPanel(data,mouseEvent){{
  var html='';
  if(data.thumbnail&&data.thumbnail.source){{
    html+='<img src="'+data.thumbnail.source
      +'" style="float:right;max-width:90px;max-height:90px;'
      +'margin:0 0 6px 8px;border-radius:3px;object-fit:cover;" />';
  }}
  html+='<strong style="font-size:13px;">'+escHtml(data.title||'')+'</strong>';
  if(data.description){{
    html+=' <span style="color:#888;font-size:10px;">'+escHtml(data.description)+'</span>';
  }}
  if(data.extract){{
    html+='<p style="margin:6px 0 0;color:#333;">'+escHtml(data.extract.slice(0,500))
      +(data.extract.length>500?'…':'')+'</p>';
  }}
  if(data.content_urls&&data.content_urls.desktop){{
    html+='<div style="margin-top:6px;clear:both;">'
      +'<a href="'+data.content_urls.desktop.page+'" target="_blank" '
      +'style="color:#0645ad;font-size:11px;pointer-events:auto;">→ Full article</a></div>';
    wikiPanel.style.pointerEvents='auto';
  }} else {{
    wikiPanel.style.pointerEvents='none';
  }}
  wikiPanel.innerHTML=html;
  wikiPanel.style.display='block';
  positionPanel(mouseEvent);
}}

function positionPanel(mouseEvent){{
  if(!mouseEvent) return;
  var x=mouseEvent.clientX+16, y=mouseEvent.clientY+16;
  var pw=wikiPanel.offsetWidth||310, ph=wikiPanel.offsetHeight||180;
  if(x+pw>window.innerWidth)  x=mouseEvent.clientX-pw-8;
  if(y+ph>window.innerHeight) y=mouseEvent.clientY-ph-8;
  wikiPanel.style.left=x+'px';
  wikiPanel.style.top=y+'px';
}}

function escHtml(s){{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

document.addEventListener('mousemove',function(e){{
  if(wikiPanel&&wikiPanel.style.display==='block'){{
    positionPanel(e);
  }}
}});

/* ═══════════════════════════════════════════════════════════
   GITHUB EXTRA PLANTS
   ═══════════════════════════════════════════════════════════ */

function fetchExtraPlants(map, svg) {{
  if (!GITHUB_OWNER || !GITHUB_REPO) return;
  var url = 'https://raw.githubusercontent.com/'
    + GITHUB_OWNER + '/' + GITHUB_REPO + '/main/metadata.json';
  fetch(url)
    .then(function(r) {{ return r.ok ? r.json() : Promise.reject(r.status); }})
    .then(function(entries) {{
      if (!Array.isArray(entries) || entries.length === 0) return;
      var rawBase = 'https://raw.githubusercontent.com/'
        + GITHUB_OWNER + '/' + GITHUB_REPO + '/main/';
      EXTRA = entries.map(function(e) {{
        return {{
          lat:            parseFloat(e.latitude)  || 0,
          lon:            parseFloat(e.longitude) || 0,
          githubUrl:      rawBase + e.github_path,
          scientific:     e.scientific_name || '',
          common:         e.common_name     || '',
          opacity:        e.confidence === 'manual' ? 0.85 : 0.5,
          color:          e.color || visitColor(e.visit_folder || ''),
          visit:          e.visit_folder || 'Upload',
          uploader:       e.uploader || '',
          nativeCountries: [],
          nonNative:      false,
          hasRange:       false,
          wikiLangs:      {{}},
          wikiUrl:        e.scientific_name
            ? 'https://en.wikipedia.org/wiki/' + e.scientific_name.replace(/ /g,'_') : '',
        }};
      }});
      updateLegend();
      render(map, svg);
    }})
    .catch(function() {{
      /* No metadata.json yet — no uploaded photos, silently ignore */
    }});
}}

function updateLegend() {{
  var seen = {{}};
  EXTRA.forEach(function(e) {{
    if (!seen[e.visit]) seen[e.visit] = e.color;
  }});
  var box = document.getElementById('legend-extra');
  if (!box) return;
  var html = '';
  Object.keys(seen).forEach(function(v) {{
    html += '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
      + 'background:' + seen[v] + ';margin-right:6px;vertical-align:middle;"></span>'
      + escHtml(v) + '<br>';
  }});
  box.innerHTML = html || '';
  box.style.display = html ? 'block' : 'none';
}}

/* ═══════════════════════════════════════════════════════════
   MAIN SVG RENDER
   ═══════════════════════════════════════════════════════════ */

var _lastPos=null, _lastCount=0;

function allPlants() {{ return PLANTS.concat(EXTRA); }}

function render(map,svg){{
  svg.innerHTML='';
  var sz=map.getSize();
  svg.setAttribute('width',sz.x);
  svg.setAttribute('height',sz.y);

  var all=allPlants();
  var pos=all.map(function(p){{
    var pt=map.latLngToContainerPoint([p.lat,p.lon]);
    return {{px:pt.x,py:pt.y,gx:pt.x,gy:pt.y}};
  }});
  layout(pos);
  _lastPos=pos;
  _lastCount=all.length;

  var labelPos=[];
  for(var i=0;i<pos.length;i++) labelPos.push(placeLabel(pos[i],pos,labelPos));

  var gPL=el('g',{{}}),gLL=el('g',{{}}),gD=el('g',{{}}),
      gI=el('g',{{}}),gHi=el('g',{{}}),gLb=el('g',{{}});
  svg.appendChild(gPL);svg.appendChild(gLL);
  svg.appendChild(gD);svg.appendChild(gI);svg.appendChild(gHi);svg.appendChild(gLb);

  for(var i=0;i<all.length;i++){{
    var p=all[i],pp=pos[i],lp=labelPos[i],op=p.opacity,co=p.color;
    var h=PHOTO/2;

    /* GPS dot */
    gD.appendChild(el('circle',{{cx:pp.gx,cy:pp.gy,r:DOT_R,fill:co,opacity:op}}));

    /* GPS → photo corner line */
    var corner=nearCorner(pp.px,pp.py,pp.gx,pp.gy);
    gPL.appendChild(el('path',{{
      d:qPath(pp.gx,pp.gy,corner[0],corner[1],CURVE),
      stroke:co,'stroke-width':'0.9',fill:'none',opacity:Math.min(1,op*0.6)
    }}));

    /* Photo — base64 for catalogue, raw GitHub URL for uploads */
    var imgHref = p.thumb
      ? 'data:image/jpeg;base64,'+p.thumb
      : (p.githubUrl || '');
    if(imgHref) {{
      gI.appendChild(el('image',{{
        href:imgHref,
        x:pp.px-h,y:pp.py-h,width:PHOTO,height:PHOTO,opacity:op
      }}));
    }}

    /* Non-native dotted amber outline */
    if(p.nonNative){{
      gHi.appendChild(el('rect',{{
        x:pp.px-h-3,y:pp.py-h-3,width:PHOTO+6,height:PHOTO+6,
        fill:'none',stroke:'#d4800a','stroke-width':'1.3',
        'stroke-dasharray':'3 3',
        opacity:Math.min(0.9,op+0.3),rx:'2'
      }}));
    }}

    /* Photo → label dashed line */
    var lCenX=lp[0]+LW/2,lCenY=lp[1]+LH/2;
    var lCorner=nearCorner(pp.px,pp.py,lCenX,lCenY);
    gLL.appendChild(el('path',{{
      d:qPath(lCorner[0],lCorner[1],lp[0],lp[1]+LH/2,CURVE*0.6),
      stroke:co,'stroke-width':'0.7',fill:'none',
      'stroke-dasharray':'3 3',opacity:Math.min(1,op*0.45)
    }}));

    /* Label */
    gLb.appendChild(el('rect',{{
      x:lp[0],y:lp[1],width:LW,height:LH,
      fill:'white','fill-opacity':Math.min(0.85,op*0.9),rx:'2'
    }}));
    var t1=el('text',{{
      x:lp[0]+3,y:lp[1]+11,
      'font-family':'Times New Roman, Times, serif','font-size':'9',
      'font-style':'italic',fill:'#1a1a1a',opacity:op
    }});
    t1.textContent=(p.scientific||'').length>20
      ?p.scientific.slice(0,19)+'…':(p.scientific||'');
    gLb.appendChild(t1);
    var labelLine2 = p.common || (p.uploader ? 'by '+p.uploader : '');
    if(labelLine2){{
      var t2=el('text',{{
        x:lp[0]+3,y:lp[1]+21,
        'font-family':'sans-serif','font-size':'8',fill:'#555',opacity:op
      }});
      t2.textContent=labelLine2.length>22?labelLine2.slice(0,21)+'…':labelLine2;
      gLb.appendChild(t2);
    }}
  }}
}}

/* ── Click handler ── */
function onContainerClick(e,map,svg){{
  if(!_lastPos) return;
  if(worldOverlay&&worldOverlay.style.display!=='none'){{
    var wd=document.getElementById('world-map-div');
    if(wd&&wd.contains(e.target)) return;
    hideOverlay(); return;
  }}
  var rect=svg.getBoundingClientRect();
  var mx=e.clientX-rect.left, my=e.clientY-rect.top;
  var h=PHOTO/2;
  var all=allPlants();
  for(var i=0;i<_lastPos.length;i++){{
    var pp=_lastPos[i];
    if(mx>=pp.px-h&&mx<=pp.px+h&&my>=pp.py-h&&my<=pp.py+h){{
      var plant=all[i];
      if(plant.hasRange) showNativeRange(plant);
      return;
    }}
  }}
}}

/* ── Init ── */
function init(){{
  var map=getMap();
  if(!map){{ setTimeout(init,400); return; }}

  var svg=document.createElementNS(NS,'svg');
  svg.style.cssText=
    'position:absolute;top:0;left:0;width:100%;height:100%;'
    +'pointer-events:none;z-index:500;overflow:visible;';
  map.getContainer().appendChild(svg);

  buildOverlay(map.getContainer());

  map.getContainer().addEventListener('click',function(e){{
    onContainerClick(e,map,svg);
  }});

  map.getContainer().addEventListener('mousemove',function(e){{
    if(!_lastPos) return;
    if(worldOverlay&&worldOverlay.style.display!=='none') return;
    var rect=svg.getBoundingClientRect();
    var mx=e.clientX-rect.left, my=e.clientY-rect.top;
    var h=PHOTO/2, cursor='';
    var all=allPlants();
    for(var i=0;i<_lastPos.length;i++){{
      var pp=_lastPos[i];
      if(mx>=pp.px-h&&mx<=pp.px+h&&my>=pp.py-h&&my<=pp.py+h){{
        if(all[i].hasRange) cursor='pointer';
        break;
      }}
    }}
    map.getContainer().style.cursor=cursor;
  }});

  map.on('zoomend moveend',function(){{ render(map,svg); }});
  render(map,svg);

  /* Fetch uploaded photos from GitHub */
  fetchExtraPlants(map, svg);
}}

document.addEventListener('DOMContentLoaded',function(){{ setTimeout(init,700); }});
}})();
</script>
<style>
.leaflet-tooltip {{ background:white;border:1px solid #ccc;font-size:11px; }}
</style>
"""

m.get_root().html.add_child(folium.Element(overlay_js))

# ── legend ────────────────────────────────────────────────────────────────────

legend_rows = "".join(
    f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
    f'background:{colour};margin-right:6px;vertical-align:middle;"></span>{name}<br>'
    for name, colour in FOLDER_COLOURS.items()
)
m.get_root().html.add_child(folium.Element(
    '<div style="position:fixed;bottom:30px;left:30px;z-index:1000;background:white;'
    'padding:12px 16px;border-radius:8px;border:2px solid #ccc;font-family:sans-serif;'
    'font-size:13px;"><b>Grain Visits</b><br>'
    + legend_rows
    + '<div id="legend-extra" style="display:none;margin-top:6px;padding-top:6px;'
    + 'border-top:1px solid #eee;"></div>'
    + '<div style="margin-top:8px;font-size:11px;color:#888;">Opacity = confidence²</div>'
    + '<div style="margin-top:4px;font-size:11px;color:#d4800a;">'
    + '▪ Dotted amber border = non-native to GB</div>'
    + '<div style="margin-top:2px;font-size:11px;color:#555;">'
    + 'Click any identified plant for native range map</div>'
    + '</div>'
))

# ── save ──────────────────────────────────────────────────────────────────────

out = ROOT / "photomap_plants_v4.html"
m.save(str(out))
print(f"\nSaved → {out}")

import subprocess
subprocess.Popen(["open", str(out)])
