// Vercel serverless function: corrige manualmente campos errados de um anúncio.
//
// Mesmo padrão de vendido.js (a página é estática, então um commit via
// Contents API é o único jeito de um clique sobreviver além do navegador),
// só que aqui o arquivo guarda um objeto {chave: {campo: valor}} em vez de
// uma lista -- cada entrada é só os campos que o usuário de fato editou.
//
// data/correcoes.json é lido a cada execução por terreno/run.py
// (aplicar_correcoes), que aplica os campos na linha e recalcula a
// pontuação -- é isso que realmente corrige o que o site mostra, não este
// arquivo por si só.
//
// Variáveis necessárias (as mesmas do vendido.js/disparar.js):
//   GITHUB_TOKEN, GITHUB_REPO, GITHUB_REF, TERRENO_SENHA

const CAMINHO = "data/correcoes.json";

// Mesma whitelist de terreno/run.py:CAMPOS_EDITAVEIS -- duplicada aqui de
// propósito: esta função nunca deve escrever no arquivo um campo que o lado
// Python não vai aceitar, e o contrário (aceitar aqui um campo que run.py
// silenciosamente ignora) seria um bug muito mais difícil de notar.
const CAMPOS_EDITAVEIS = ["title", "price", "area_ha", "municipality", "uf", "description"];

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ erro: "Use POST." });
  }

  const senhaEsperada = process.env.TERRENO_SENHA;
  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.GITHUB_REPO;
  const ref = process.env.GITHUB_REF || "main";

  if (!senhaEsperada || !token || !repo) {
    return res.status(500).json({
      erro: "Função sem configuração. Falta GITHUB_TOKEN, GITHUB_REPO ou TERRENO_SENHA.",
    });
  }

  const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
  const { senha, key, campos } = body;

  if (senha !== senhaEsperada) {
    return res.status(401).json({ erro: "Senha incorreta." });
  }
  if (!key || typeof key !== "string") {
    return res.status(400).json({ erro: "Faltou a chave do anúncio." });
  }
  if (!campos || typeof campos !== "object" || Array.isArray(campos)) {
    return res.status(400).json({ erro: "Faltaram os campos a corrigir." });
  }

  const limpos = {};
  for (const nome of CAMPOS_EDITAVEIS) {
    if (Object.prototype.hasOwnProperty.call(campos, nome)) {
      limpos[nome] = campos[nome];
    }
  }
  if (Object.keys(limpos).length === 0) {
    return res.status(400).json({ erro: "Nenhum campo reconhecido para corrigir." });
  }

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  const url = `https://api.github.com/repos/${repo}/contents/${CAMINHO}?ref=${ref}`;

  let atuais = {};
  let sha;
  const leitura = await fetch(url, { headers });
  if (leitura.status === 200) {
    const dados = await leitura.json();
    sha = dados.sha;
    try {
      atuais = JSON.parse(Buffer.from(dados.content, "base64").toString("utf-8")) || {};
    } catch {
      atuais = {};
    }
  } else if (leitura.status !== 404) {
    const detalhe = await leitura.text();
    return res.status(502).json({
      erro: `GitHub respondeu ${leitura.status} ao ler ${CAMINHO}`,
      detalhe: detalhe.slice(0, 300),
    });
  }

  // Mescla por campo, não substitui o anúncio inteiro -- uma segunda edição
  // só de preço não deve apagar uma correção de município feita antes.
  atuais[key] = { ...(atuais[key] || {}), ...limpos };

  const conteudo = JSON.stringify(atuais, null, 1);
  const escrita = await fetch(url, {
    method: "PUT",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `correção: ${key}`,
      content: Buffer.from(conteudo, "utf-8").toString("base64"),
      branch: ref,
      ...(sha ? { sha } : {}),
    }),
  });

  if (escrita.status === 200 || escrita.status === 201) {
    return res.status(200).json({
      ok: true,
      mensagem: "Correção gravada. Aparece no site na próxima execução.",
    });
  }

  const detalhe = await escrita.text();
  return res.status(502).json({
    erro: `GitHub respondeu ${escrita.status} ao gravar ${CAMINHO}`,
    detalhe: detalhe.slice(0, 300),
  });
}
