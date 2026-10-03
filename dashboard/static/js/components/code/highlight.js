/**
 * Coloration syntaxique minimale : un analyseur lexical à règles (expressions régulières
 * « collantes ») par langage. Suffisant pour JSON, Python, Protobuf, JavaScript, HTTP et shell ;
 * tout autre langage est rendu tel quel.
 */

/**
 * @typedef {Object} Token
 * @property {'plain'|'keyword'|'string'|'number'|'comment'|'property'|'function'|'type'|'constant'|'punct'|'meta'} type
 * @property {string} text
 */

const STRING_DQ = /"(?:[^"\\\n]|\\.)*"?/y;
const STRING_SQ = /'(?:[^'\\\n]|\\.)*'?/y;
const NUMBER = /-?\b(?:0[xX][\da-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/y;
const WORD = /[A-Za-z_$][\w$]*/y;
const SPACE = /\s+/y;

const RULES = {
  json: [
    ['property', /"(?:[^"\\\n]|\\.)*"(?=\s*:)/y],
    ['string', STRING_DQ],
    ['number', NUMBER],
    ['constant', /\b(?:true|false|null)\b/y],
    ['punct', /[{}[\],:]/y],
    ['plain', SPACE],
  ],
  python: [
    ['comment', /#[^\n]*/y],
    ['string', /[rRbBfFuU]{0,2}(?:"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$))/y],
    ['string', /[rRbBfFuU]{0,2}(?:"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?)(?![A-Za-z_])/y],
    ['meta', /@[A-Za-z_][\w.]*/y],
    [
      'keyword',
      /\b(?:def|class|return|if|elif|else|for|while|in|not|and|or|is|import|from|as|with|try|except|finally|raise|pass|break|continue|lambda|yield|global|nonlocal|assert|del|async|await)\b/y,
    ],
    ['constant', /\b(?:True|False|None|self|cls)\b/y],
    ['function', /(?<=\bdef\s+)[A-Za-z_]\w*/y],
    ['type', /(?<=\bclass\s+)[A-Za-z_]\w*/y],
    ['function', /[A-Za-z_]\w*(?=\s*\()/y],
    ['number', NUMBER],
    ['punct', /[()[\]{}.,:;=+\-*/%<>!&|^~]/y],
    ['plain', WORD],
    ['plain', SPACE],
  ],
  protobuf: [
    ['comment', /\/\/[^\n]*/y],
    ['comment', /\/\*[\s\S]*?(?:\*\/|$)/y],
    ['string', STRING_DQ],
    ['keyword', /\b(?:syntax|package|import|option|message|service|rpc|returns|stream|enum|repeated|optional|required|oneof|map|reserved|extend|public|weak)\b/y],
    ['type', /\b(?:double|float|int32|int64|uint32|uint64|sint32|sint64|fixed32|fixed64|sfixed32|sfixed64|bool|string|bytes)\b/y],
    ['constant', /\b(?:true|false)\b/y],
    ['function', /(?<=\brpc\s+)[A-Za-z_]\w*/y],
    ['type', /\b[A-Z]\w*/y],
    ['property', /[a-z_]\w*(?=\s*=\s*\d)/y],
    ['number', NUMBER],
    ['punct', /[{}()[\]<>=;,.]/y],
    ['plain', WORD],
    ['plain', SPACE],
  ],
  javascript: [
    ['comment', /\/\/[^\n]*/y],
    ['comment', /\/\*[\s\S]*?(?:\*\/|$)/y],
    ['string', /`(?:[^`\\]|\\.)*`?/y],
    ['string', STRING_DQ],
    ['string', STRING_SQ],
    [
      'keyword',
      /\b(?:const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|import|from|export|default|async|await|try|catch|finally|throw|typeof|instanceof|in|of|this|yield|delete|void)\b/y,
    ],
    ['constant', /\b(?:true|false|null|undefined|NaN|Infinity)\b/y],
    ['function', /[A-Za-z_$][\w$]*(?=\s*\()/y],
    ['property', /[A-Za-z_$][\w$]*(?=\s*:)/y],
    ['number', NUMBER],
    ['punct', /[()[\]{}.,:;=+\-*/%<>!&|^~?]/y],
    ['plain', WORD],
    ['plain', SPACE],
  ],
  shell: [
    ['comment', /#[^\n]*/y],
    ['meta', /^(?:PS>|\$|>)(?= )/my],
    ['string', STRING_DQ],
    ['string', STRING_SQ],
    ['constant', /\$\{[^}\n]*\}|\$[A-Za-z_]\w*|%[A-Za-z_]\w*%/y],
    ['property', /(?<=\s)--?[A-Za-z][\w-]*/y],
    ['keyword', /\b(?:if|then|else|elif|fi|for|do|done|while|case|esac|in|function|export|set)\b(?![.\-/])/y],
    ['number', /\b\d+(?:\.\d+)?\b(?![\w.\-/])/y],
    ['punct', /[|&;<>()=]/y],
    ['word', /[\w./:@\\~+-]+/y],
    ['plain', SPACE],
  ],
};

const ALIASES = { js: 'javascript', mjs: 'javascript', py: 'python', proto: 'protobuf', bash: 'shell', sh: 'shell', powershell: 'shell', console: 'shell', jsonrpc: 'json' };

function lex(code, rules) {
  /** @type {Token[]} */
  const tokens = [];
  let position = 0;
  let plain = '';
  const flush = () => {
    if (plain) tokens.push({ type: 'plain', text: plain });
    plain = '';
  };
  while (position < code.length) {
    let matched = null;
    for (const [type, regex] of rules) {
      regex.lastIndex = position;
      const match = regex.exec(code);
      if (match && match[0].length) {
        matched = { type, text: match[0] };
        break;
      }
    }
    if (!matched) {
      plain += code[position];
      position += 1;
    } else if (matched.type === 'plain') {
      plain += matched.text;
      position += matched.text.length;
    } else {
      flush();
      tokens.push(matched);
      position += matched.text.length;
    }
  }
  flush();
  return tokens;
}

/** Shell : le premier mot de chaque commande (début de ligne, après `|`, `&&`, `;`) est la commande. */
function lexShell(code) {
  let expectCommand = true;
  return lex(code, RULES.shell).map((token) => {
    if (token.type === 'plain') {
      if (token.text.includes('\n')) expectCommand = true;
      return token;
    }
    if (token.type === 'punct') {
      if (/[|&;(]/.test(token.text)) expectCommand = true;
      return token;
    }
    if (token.type === 'word') {
      const isCommand = expectCommand;
      expectCommand = false;
      return { type: isCommand ? 'function' : 'plain', text: token.text };
    }
    if (token.type !== 'meta' && token.type !== 'comment') expectCommand = false;
    return token;
  });
}

/** HTTP/1.1 : ligne de requête ou de statut, en-têtes, puis corps (coloré comme du JSON s'il en est). */
function lexHttp(code) {
  const tokens = [];
  const lines = code.split('\n');
  let index = 0;
  for (; index < lines.length; index += 1) {
    const line = lines[index];
    const newline = index < lines.length - 1 ? '\n' : '';
    if (line.trim() === '') {
      tokens.push({ type: 'plain', text: line + newline });
      index += 1;
      break;
    }
    const request = /^([A-Z]+)(\s+)(\S+)(\s+)(HTTP\/[\d.]+)(\s*)$/.exec(line);
    const status = /^(HTTP\/[\d.]+)(\s+)(\d{3})(.*)$/.exec(line);
    const header = /^([A-Za-z0-9-]+)(:)(.*)$/.exec(line);
    if (index === 0 && request) {
      tokens.push({ type: 'keyword', text: request[1] }, { type: 'plain', text: request[2] }, { type: 'string', text: request[3] }, { type: 'plain', text: request[4] }, { type: 'meta', text: request[5] }, { type: 'plain', text: request[6] + newline });
    } else if (index === 0 && status) {
      tokens.push({ type: 'meta', text: status[1] }, { type: 'plain', text: status[2] }, { type: 'number', text: status[3] }, { type: 'plain', text: status[4] + newline });
    } else if (header) {
      tokens.push({ type: 'property', text: header[1] }, { type: 'punct', text: header[2] }, { type: 'plain', text: header[3] + newline });
    } else {
      tokens.push({ type: 'plain', text: line + newline });
    }
  }
  const body = lines.slice(index).join('\n');
  if (body) tokens.push(...(/^\s*[{[]/.test(body) ? lex(body, RULES.json) : [{ type: 'plain', text: body }]));
  return tokens;
}

/**
 * Nom canonique d'un langage (`js` → `javascript`, `proto` → `protobuf`, `bash` → `shell`…).
 * @param {string} [language]
 * @returns {string}
 */
export function languageName(language) {
  const name = String(language ?? 'text').toLowerCase();
  return ALIASES[name] ?? name;
}

/**
 * Découpe un code source en lignes de jetons colorables.
 * @param {string} code
 * @param {string} [language] `json`, `python`, `protobuf`, `javascript`, `http`, `shell` (ou alias) ; sinon texte brut.
 * @returns {Token[][]} Une liste de jetons par ligne (les retours à la ligne sont retirés).
 */
export function highlight(code, language) {
  const source = String(code ?? '').replace(/\r\n?/g, '\n');
  const name = languageName(language);
  let tokens;
  if (name === 'http') tokens = lexHttp(source);
  else if (name === 'shell') tokens = lexShell(source);
  else if (RULES[name]) tokens = lex(source, RULES[name]);
  else tokens = [{ type: 'plain', text: source }];

  const lines = [[]];
  for (const token of tokens) {
    const parts = token.text.split('\n');
    parts.forEach((part, index) => {
      if (index > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ type: token.type, text: part });
    });
  }
  return lines;
}
