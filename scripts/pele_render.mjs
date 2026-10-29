// Render a batch of Mermaid sources to SVG with a pinned Pele bundle.
//
// Usage: node scripts/pele_render.mjs <absolute path to pele.mjs>
// (Pele's dist/pele.js, cached under a .mjs name so every Node.js >= 20
// loads it as an ES module.)
// stdin:  {"diagrams": ["<mermaid source>", ...], "options": {...}}
// stdout: {"results": [{"svg", "type", "width", "height"} | {"error": {...}}]}
//
// Results keep the input order. A diagram that fails to render is reported as
// data ({"error": {"code", "message", "line", "column"}}) and the process still
// exits 0. Exit 2 means a usage error or a Node.js version below 20; any other
// non-zero exit is a crash. Called by scripts/pele_renderer.py.
import { pathToFileURL } from 'node:url';

const MIN_NODE_MAJOR = 20;
const PELE_CODES = new Set(['unsupported-diagram', 'syntax', 'semantic', 'limit']);

function usage(message) {
  process.stderr.write(`pele_render: ${message}\n`);
  process.exit(2);
}

const nodeMajor = Number(process.versions.node.split('.')[0]);
if (!(nodeMajor >= MIN_NODE_MAJOR)) {
  usage(`Node.js >= ${MIN_NODE_MAJOR} is required (found ${process.versions.node})`);
}
if (process.argv.length !== 3) {
  usage('expected exactly one argument: the path to pele.mjs');
}

const { render } = await import(pathToFileURL(process.argv[2]).href);
if (typeof render !== 'function') {
  usage(`${process.argv[2]} does not export render()`);
}

let input = '';
process.stdin.setEncoding('utf8');
for await (const chunk of process.stdin) {
  input += chunk;
}

let request;
try {
  request = JSON.parse(input);
} catch (err) {
  usage(`stdin is not valid JSON: ${err.message}`);
}
if (!request || !Array.isArray(request.diagrams)) {
  usage('stdin must be an object with a "diagrams" array');
}
const options = request.options && typeof request.options === 'object' ? request.options : {};

function toError(err) {
  const known = err !== null && typeof err === 'object' && PELE_CODES.has(err.code);
  const message = err !== null && typeof err === 'object' && 'message' in err ? err.message : err;
  return {
    code: known ? err.code : 'internal',
    message: String(message),
    line: known && Number.isInteger(err.line) ? err.line : null,
    column: known && Number.isInteger(err.column) ? err.column : null,
  };
}

const results = request.diagrams.map((source) => {
  try {
    const out = render(String(source), options);
    return { svg: out.svg, type: out.type, width: out.width, height: out.height };
  } catch (err) {
    return { error: toError(err) };
  }
});

process.stdout.write(JSON.stringify({ results }));
