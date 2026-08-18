// Vercel serverless function: marca um anúncio como vendido/indisponível.
//
// A página é estática (site/index.html + listings.json) e não tem servidor
// próprio, então o único jeito de um clique nela sobreviver além do
// navegador é um commit no repositório -- o mesmo padrão do disparar.js,
// mas usando a Contents API do GitHub em vez de disparar um workflow.
//
// data/vendidos.json guarda só a lista de chaves marcadas. terreno/run.py
// lê esse arquivo no início de cada execução e transforma cada chave num
// `dismissed = 1` na base -- é isso que de fato tira o anúncio de todo
// build futuro do site, não este arquivo por si só.
//
// Variáveis necessárias (as mesmas do disparar.js):
//   GITHUB_TOKEN, GITHUB_REPO, GITHUB_REF, TERRENO_SENHA

const CAMINHO = "data/vendidos.json";

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
  const { senha, key } = body;

  if (senha !== senhaEsperada) {
    return res.status(401).json({ erro: "Senha incorreta." });
  }
  if (!key || typeof key !== "string") {
    return res.status(400).json({ erro: "Faltou a chave do anúncio." });
  }

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  const url = `https://api.github.com/repos/${repo}/contents/${CAMINHO}?ref=${ref}`;

  let atuais = [];
  let sha;
  const leitura = await fetch(url, { headers });
  if (leitura.status === 200) {
    const dados = await leitura.json();
    sha = dados.sha;
    try {
      atuais = JSON.parse(Buffer.from(dados.content, "base64").toString("utf-8")).keys || [];
    } catch {
      atuais = [];
    }
  } else if (leitura.status !== 404) {
    const detalhe = await leitura.text();
    return res.status(502).json({
      erro: `GitHub respondeu ${leitura.status} ao ler ${CAMINHO}`,
      detalhe: detalhe.slice(0, 300),
    });
  }

  if (atuais.includes(key)) {
    return res.status(200).json({ ok: true, mensagem: "Já estava marcado como vendido." });
  }

  const conteudo = JSON.stringify({ keys: [...atuais, key] }, null, 1);
  const escrita = await fetch(url, {
    method: "PUT",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `vendido: ${key}`,
      content: Buffer.from(conteudo, "utf-8").toString("base64"),
      branch: ref,
      ...(sha ? { sha } : {}),
    }),
  });

  if (escrita.status === 200 || escrita.status === 201) {
    return res.status(200).json({
      ok: true,
      mensagem: "Marcado como vendido. Sai do site na próxima execução.",
    });
  }

  const detalhe = await escrita.text();
  return res.status(502).json({
    erro: `GitHub respondeu ${escrita.status} ao gravar ${CAMINHO}`,
    detalhe: detalhe.slice(0, 300),
  });
}
