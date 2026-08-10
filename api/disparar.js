// Vercel serverless function: dispara uma busca manual no GitHub Actions.
//
// Existe para que o token do GitHub nunca chegue ao navegador. A página envia
// os critérios e uma senha; esta função valida a senha e chama a API do GitHub
// com o token guardado nas variáveis de ambiente da Vercel.
//
// Variáveis necessárias (Vercel → Settings → Environment Variables):
//   GITHUB_TOKEN   fine-grained PAT com permissão Actions: read and write
//                  apenas no repositório AtisMatiz/Terreno
//   GITHUB_REPO    AtisMatiz/Terreno
//   GITHUB_REF     claude/land-search-scraper-evdukq  (branch a rodar)
//   TERRENO_SENHA  qualquer frase; sem ela a página não dispara nada

const WORKFLOW = "search.yml";

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
  const { senha, criterios, perfil, salvar } = body;

  // Comparison is intentionally simple: this guards a search trigger, not
  // anything destructive. The real protection is that the token never leaves
  // the server.
  if (senha !== senhaEsperada) {
    return res.status(401).json({ erro: "Senha incorreta." });
  }

  // Only the adjustable criteria may be overridden. Anything else the page
  // might send is dropped rather than forwarded, so a modified page cannot
  // reach into budgets, sources or profiles.
  const permitido = {};
  if (criterios && typeof criterios === "object") {
    for (const chave of ["localizacao", "area", "preco", "max_preco_por_ha"]) {
      if (criterios[chave] !== undefined) permitido[chave] = criterios[chave];
    }
  }

  const resposta = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref,
        inputs: {
          profile: perfil === "local" ? "local" : "ci",
          overrides: Object.keys(permitido).length ? JSON.stringify(permitido) : "",
          salvar: salvar ? "true" : "false",
        },
      }),
    }
  );

  if (resposta.status === 204) {
    return res.status(200).json({
      ok: true,
      mensagem: "Busca disparada. Os resultados aparecem aqui em alguns minutos.",
    });
  }

  const detalhe = await resposta.text();
  return res.status(502).json({
    erro: `GitHub respondeu ${resposta.status}`,
    detalhe: detalhe.slice(0, 300),
  });
}
