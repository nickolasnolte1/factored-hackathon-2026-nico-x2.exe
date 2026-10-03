"""Keyword and regex intent router: baseline (b) of report 02, section 10.

A rule-based router for the six intake intents, kept as the reference point that the learned classifier has to beat.

How it was written:

- The glossary below comes from the class definitions (report 03, section 3; report 02, section 10) and from common
  banking vocabulary of customers in Mexico, Colombia, Argentina and Brazil (slang, abbreviations, missing accents).
  Rules are vocabulary and short constructions, not memorized sentences, and they name no merchant.
- Coverage was checked on the train split only: messages that matched no rule, or the wrong one, were read to find
  missing general terms (typos were left alone). Dev was scored three times as a sanity number and its messages were
  never read. The rules were never tuned on the test split or on the independent holdout, and this module reads no
  data file. Train scores are therefore optimistic; report dev, test and holdout numbers only.
- The ES and PT lists of each rule group mirror each other: a term or construction found for one language was also
  written for the other, so some vocabulary has no counterpart in the training rows.

Text is normalized before matching: lowercase, accents stripped ("cartão" = "cartao", "débito" = "debito"),
punctuation turned into spaces, whitespace collapsed and a few chat abbreviations expanded ("q" -> "que",
"tdc" -> "tarjeta de credito"). ES and PT patterns are applied to every message, so portunhol works with both lists.

Resolution order (the first rule group that matches wins, see RESOLUTION_ORDER):

1. Requests for another customer's data, and claimed authority -> out_of_scope (the agent refuses them).
2. Lost, stolen, swallowed or cloned card, or a block request -> card_lost_or_block. It comes before the disputes
   because a cloned card with purchases the customer did not make is a card case first.
3. A charge the customer did not make -> dispute_unrecognized_charge.
4. A charge that is theirs but wrong (duplicate, overcharge, fee or interest, ATM without cash, charged after
   cancelling, instalments charged at once) -> dispute_incorrect_charge_or_fee. A fee word counts only next to a
   charge verb or a word such as "indebido": a fee mentioned without being charged is left to the later groups, so a
   question about loan rates ends in out_of_scope and a rant about fees in other_complaint.
5. A dispute with no detail ("quero contestar uma cobranca") -> dispute_unrecognized_charge: in the end-to-end plan
   (report 03, section 6) a first message without details belongs to the unrecognized category and is clarified.
6. Instructions aimed at the assistant, or asking it to skip a check -> out_of_scope. Steps 2-5 run first, so a real
   dispute with injected text keeps its intent, and the agent serves it while ignoring the injection.
7. A refund or reversal request with no attack -> dispute_incorrect_charge_or_fee. It sits after step 6 because
   injected messages often ask for a refund.
8. Staff, wait times, unreachable service, app or ATM failures, marketing calls -> other_complaint.
9. Loans, limit increases, investments, insurance, new accounts, data changes, branch hours, small talk ->
   out_of_scope. These rules are written as requests ("quiero un prestamo"), so a question about a loan payment still
   reaches step 10.
10. Balance, movements, statements, declines, payment status, "what is this charge" -> account_payment_inquiry.
11. Any other mention of a charge, payment, card or account -> account_payment_inquiry, the class the labeling
    convention gives to a charge question with no clear complaint.
12. Nothing matched -> DEFAULT_INTENT (out_of_scope): the agent abstains and offers a person, which is the safe
    behavior, and out_of_scope is also the largest class of the train split.

    python -m src.classifier.keywords "me cobraron dos veces el super"     # print the intent and the rules that fired
"""
import argparse
import re
import unicodedata

INTENTS = ("dispute_unrecognized_charge", "dispute_incorrect_charge_or_fee", "account_payment_inquiry",
           "card_lost_or_block", "other_complaint", "out_of_scope")
DEFAULT_INTENT = "out_of_scope"

ABBREVIATIONS = {
    "q": "que", "k": "que", "xq": "porque", "pq": "porque", "porq": "porque", "vc": "voce", "n": "nao",
    "tdc": "tarjeta de credito", "tdd": "tarjeta de debito", "targeta": "tarjeta", "tarj": "tarjeta", "cel": "celular",
    "movs": "movimientos", "mov": "movimiento", "nro": "numero", "num": "numero", "msm": "mesmo", "hj": "hoje",
}

# Shared vocabulary, used inside the patterns below.
_CARD_ES = r"(tarjetas?|plasticos?)"
_CARD_PT = r"(cartao|cartoes)"
_WALLET = r"(cartera|billetera|bolso|mochila|celular|telefono|carteira|bolsa|documentos|cartao|tarjeta)"
_CHARGE_ES = r"(cobr\w*|carg\w*|debit\w*|descont\w*|factur\w*|aparec\w*)"
_CHARGE_PT = r"(cobr\w*|debit\w*|descont\w*|lanc\w*)"
# Fees, interest, exchange rates and "it was free": a dispute only next to a charge verb.
_FEE_ES = (r"(comision\w*|cuota de manejo|cuota de administracion|anualidad\w*|interes|intereses|recargo\w*|"
           r"penalizacion\w*|multa\w*|costo de (manejo|mantenimiento|administracion)|cargo por \w+|cuatro por mil|"
           r"4 por mil|gmf|iva|impuesto\w*|tipo de cambio|tasa de cambio|cotizacion|sin costo|no tienen? costo|"
           r"gratis|gratuit\w*)")
_FEE_PT = (r"(tarifa\w*|taxa\w*|anuidade\w*|juros|encargo\w*|multa\w*|iof|mensalidade\w*|cesta de servicos|"
           r"comissao|cotacao|cambio|sem custo|gratis|gratuit\w*)")
_PAYOBJ_ES = (r"(pagos?|transferencias?|depositos?|abonos?|giros?|consignacion\w*|nomina|sueldo|spei|remesa\w*|"
              r"pague|pagamos|transferi|deposite|envie|mande|consigne)")
_PAYOBJ_PT = (r"(pagamentos?|transferencias?|pix|ted|depositos?|boletos?|salario|paguei|transferi|enviei|depositei|"
              r"mandei)")
_APP = (r"(app|aplicacion|aplicativo|pagina|web|sitio|site|portal|banca (en linea|movil|digital|online|virtual)|"
        r"home ?banking|internet banking)")
_FAIL_ES = (r"(no (me )?(funciona|abre|carga|sirve|anda|deja|responde)|se (cae|cayo|traba|trabo|cierra|colgo|tilda|"
            r"tildo)|falla\w*|error\w*|caid[ao]|lentisim[ao]|bug\w*)")
_FAIL_PT = r"(nao (funciona|abre|carrega|entra|deixa|responde)|trav\w*|caiu|fora do ar|bug\w*|erro\w*|fecha sozinho)"
_THIRD_ES = (r"(esposa|esposo|exesposa|exesposo|marido|mujer|novia|novio|hermana|hermano|mama|papa|madre|padre|hija|"
             r"hijo|tia|tio|suegra|suegro|vecina|vecino|jefe|jefa|socia|socio|amiga|amigo|companero|companera|"
             r"cliente|clientes|persona|titular)")
_THIRD_PT = (r"(esposa|esposo|marido|mulher|namorada|namorado|irma|irmao|mae|pai|filha|filho|tia|tio|sogra|sogro|"
             r"vizinha|vizinho|chefe|socia|socio|amiga|amigo|colega|cliente|clientes|pessoa|titular)")
_PERSONAL_DATA = (r"(saldo|movimientos|datos|nombre|telefono|celular|direccion|correo|cedula|documento|dni|curp|rfc|"
                  r"extracto|estado de cuenta|numero|extrato|movimentacoes|dados|nome|endereco|email|cpf|rg)")
_ROLE = (r"(gerente|director|directora|diretor|diretora|auditor|auditora|funcionario|funcionaria|empleado|empleada|"
         r"ejecutivo|ejecutiva|analista|supervisor|supervisora|policia|policial|delegado|delegada|abogado|abogada|"
         r"advogado|advogada|fiscal|promotor|juez|juiz|juiza)")

GLOSSARY = {
    # Attacks first: requests for someone else's data, claimed authority, and instructions to the assistant.
    "other_customer_data": {
        "es": [
            rf"\b{_PERSONAL_DATA}\b.{{0,40}}\bde(l)? (mi |mis |un |una |otro |otra |la |el |esa |ese )?{_THIRD_ES}\b",
            rf"\bmi (ex )?{_THIRD_ES}\b.{{0,50}}\b(su|sus) (cuenta|tarjeta|saldo|compras|movimientos)\b",
            r"\bcuanto (tiene|debe|gasto|gana|le (entro|queda|llego|deposito))\b",
            r"\b(otro cliente|otros clientes|todos los clientes|cualquier cliente|un tercero)\b",
            r"\bquien (me )?(transfirio|deposito|envio|mando|pago|giro|consigno)\b",
            r"\b(remitente|ordenante|destinatario|beneficiario)\b",
            r"\b(a quien (le )?pertenece|de quien es (la|esta|esa) cuenta|"
            r"(dueno|duena|titular) de (la|esa|esta) cuenta)\b",
        ],
        "pt": [
            rf"\b{_PERSONAL_DATA}\b.{{0,40}}\bd(e|o|a|os|as) (meu |minha |um |uma |outro |outra |o |a |esse |essa )?"
            rf"{_THIRD_PT}\b",
            r"\b(conta|cartao|saldo|compras|extrato|fatura) (dele|dela|deles|delas)\b",
            r"\bquanto (ele|ela|eles|elas) (tem|deve|gastou|recebeu|ganha)\b",
            r"\b(outro cliente|outros clientes|todos os clientes|qualquer cliente|terceiros)\b",
            r"\bquem (me )?(transferiu|depositou|enviou|mandou|pagou|fez (o|um|esse|essa|aquele) pix)\b",
            r"\b(remetente|pagador|favorecido|destinatario)\b",
            r"\b(a quem pertence|de quem e (a|essa|esta) conta|(dono|dona|titular) da conta)\b",
        ],
    },
    "social_engineering": {
        "es": [
            rf"\b(soy|habla|le habla|te habla|como) (el |la |un |una )?{_ROLE}\b",
            r"\b(soy|somos|hablo|llamo|escribo|aqui|estoy hablando) (es )?(del|de la) (banco|area|departamento|equipo|"
            r"soporte|seguridad|fraudes|sistemas|auditoria|policia|fiscalia|gerencia|central)\b",
            r"\btrabajo (en|para) (el|este) banco\b",
            r"\b(orden judicial|oficio judicial|requerimiento (legal|judicial)|por orden de|(orden|pedido) de la "
            r"(gerencia|direccion|presidencia))\b",
            r"\b(le|te|yo) (lo )?(ordeno|autorizo)\b",
            r"\b(cuenta (de seguridad|segura)|acceso al sistema)\b",
        ],
        "pt": [
            rf"\b(sou|aqui e|fala|falando|como) (o |a |um |uma )?{_ROLE}\b",
            r"\b(sou|somos|falo|ligo|escrevo|aqui e|estou falando) (do|da) (banco|area|departamento|equipe|suporte|"
            r"seguranca|setor|auditoria|policia|central|diretoria)\b",
            r"\btrabalho (no|neste|para o) banco\b",
            r"\b(ordem judicial|mandado judicial|oficio judicial|determinacao judicial|(ordem|pedido) da "
            r"(diretoria|gerencia|presidencia))\b",
            r"\b(eu (te )?(ordeno|autorizo))\b",
            r"\b(conta (de seguranca|segura)|acesso ao sistema)\b",
        ],
    },
    "card_lost_or_block": {
        "es": [
            rf"\b(perdi|perdida|perdido|se me (perdio|cayo|quedo)|no encuentro|olvide|deje)\b.{{0,40}}"
            rf"\b({_CARD_ES}|cartera|billetera)\b",
            rf"\b{_CARD_ES}\b.{{0,30}}\b(perdid[ao]|robad[ao]|hurtad[ao]|ya no estaba)\b",
            r"\bextravi\w*\b",
            rf"\b(me|nos) (robaron|hurtaron|bolsearon|quitaron|arrebataron) (la |el |mi |mis |los )?{_WALLET}",
            r"\b(me|nos) (asaltaron|atracaron|carterearon)\b",
            r"\b(robo|hurto|asalto|atraco)\b.{0,40}\b(tarjeta|cartera|billetera|celular|bolso)\b",
            r"\b(des)?bloque\w*\b",
            rf"\b(congel\w*|inhabilit\w*|desactiv\w*|suspend\w*|pausar|apagar)\b.{{0,30}}\b{_CARD_ES}\b",
            r"\b(darla de baja|la (das|den|dan|des|podes dar|puedes dar|pueden dar) de baja|de baja (la|mi) tarjeta)\b",
            rf"\b(se trago|trago|se comio|retuvo|se quedo con|atrapo)\b.{{0,15}}\b{_CARD_ES}\b",
            r"\bclon\w*\b",
            r"\b(copiaron|robaron|filtraron|hackearon) (los |mis )?datos\b|\bdatos de (la|mi) tarjeta\b",
            r"\b(intent\w*)\b.{0,25}\b(usar|compras?|hacer compras)\b",
            r"\b(reposicion|reponer (la|mi) tarjeta)\b",
        ],
        "pt": [
            rf"\b(perdi|perdeu|perdid[ao]|nao (acho|encontro)|sumiu|esqueci|deixei)\b.{{0,40}}"
            rf"\b({_CARD_PT}|carteira)\b",
            rf"\b{_CARD_PT}\b.{{0,30}}\b(perdid[ao]|roubad[ao]|furtad[ao]|sumiu)\b",
            r"\bperda (do|de) (meu |o )?cartao\b",
            rf"\b(roubaram|furtaram|levaram|pegaram) (o |a |meu |minha |meus |minhas )?{_WALLET}",
            r"\b(me assaltaram|fui (roubad|furtad|assaltad)\w*)\b",
            r"\b(roubo|furto|assalto)\b.{0,40}\b(cartao|carteira|celular|bolsa)\b",
            r"\bbloqui\w*\b",
            rf"\b(congel\w*|desativ\w*|suspend\w*|trav(ar|a|e|em))\b.{{0,30}}\b{_CARD_PT}\b",
            rf"\b(engoliu|reteve|prendeu|comeu|ficou com|segurou)\b.{{0,15}}\b{_CARD_PT}\b",
            r"\b(clonag\w*|clonaram|clonad[ao])\b",
            r"\b(roubaram|copiaram|vazaram|clonaram) (os |meus )?dados\b",
            r"\b(tentaram|tentando|tentativas?)\b.{0,25}\b(usar|compras?)\b",
            r"\b(emissao de (um )?novo|segunda via do cartao)\b",
        ],
    },
    "dispute_unrecognized_charge": {
        "es": [
            r"\b(no|nunca|jamas) (lo |la |los |las )?(reconozco|reconoci|identifico|ubico)\b",
            r"\bdesconoc\w*\b",
            r"\bno (lo |la |los |las |he )?(hice|hecho|realice|realizado|autorice|autorizado|efectue|solicite|contrate|"
            r"pedi|comprado)\b",
            r"\bno (compre|pague|retire|saque|transferi)( yo)? (esa|ese|esta|este|eso|esto|nada|ninguna|ningun|"
            r"nunca)\b",
            r"\bque (yo )?no (compre|hice|realice|pedi|use)\b",
            r"\b(nunca|jamas) (me )?(compre|he comprado|hice|realice|autorice|pague|retire|saque|suscribi|registre|"
            r"solicite|pedi|contrate|inscribi|uso|use)\b",
            r"\b(nunca|jamas) (he )?(ido|estado|estuve|fui) (a|al|en|ahi|alli)\b",
            r"\b(no (fui|fue|he sido) yo|yo no fui|no fuimos)\b",
            r"\bno (es|son) (mio|mia|mios|mias|nuestro|nuestra|nuestros|nuestras)\b",
            r"\b(sin (mi )?(autorizacion|permiso|consentimiento|conocimiento)|sin que yo (lo |la )?(haya )?"
            r"(pedido|autorizado|solicitado))\b",
            r"\bno (fue )?(autorizad|reconocid)\w*\b",
            r"\b(fraude\w*|fraudulent\w*|hackea\w*|jaquea\w*|suplant\w*|robo de identidad|phishing)\b",
            r"\b(alguien|nadie)\b.{0,25}\b(uso|hizo|realizo|saco|retiro|compro|transfirio|pago|gasto|reconoce)\b",
            r"\b(me )?(sacaron|retiraron|vaciaron|robaron|quitaron) (la |el )?(plata|dinero|lana|guita|todo)\b",
            r"\b(cargo|cobro|compra|movimiento|retiro|transaccion|transferencia|consumo|debito)s? (raro|rara|raros|"
            r"raras|extran\w*|sospechos\w*|desconocid\w*|fantasma)\b",
            r"\b(tarjeta\b.{0,30}\b(nunca salio|conmigo)|no (saque|retire) (plata|dinero|efectivo|nada))\b",
            r"\bno conozco (ese|esa|este|esta|esos|esas|el|la|a)\b",
        ],
        "pt": [
            r"\b(nao|nunca) (reconheco|reconheci|identifico|conheco|conheci)\b",
            r"\bdesconhe\w*\b",
            r"\b(eu )?nao (fiz|realizei|autorizei|efetuei|solicitei|contratei|pedi)\b",
            r"\bnao (comprei|paguei|saquei|transferi)( isso| isto)? (essa|esse|esta|este|isso|isto|nada|nenhum|nenhuma"
            r"|nunca|la|ali)\b",
            r"\bque (eu )?nao (comprei|fiz|realizei|pedi|usei)\b",
            r"\b(nunca|nem) (comprei|fiz|realizei|autorizei|paguei|saquei|usei|uso|assinei|pedi|contratei)\b",
            r"\bnunca (fui|estive|passei) (a|ao|na|no|em|nesse|nessa|la|ali)\b",
            r"\b(nao fui eu|nao fomos)\b",
            r"\bnao (e|sao) (meu|minha|meus|minhas|nosso|nossa|nossos|nossas|de ninguem)\b",
            r"\b(sem (minha |a minha )?(autorizacao|permissao|consentimento|conhecimento)|sem que eu (tenha )?"
            r"(pedido|autorizado|solicitado))\b",
            r"\bnao (foi )?(autorizad|reconhecid)\w*\b",
            r"\b(fraude\w*|fraudulent\w*|golpe\w*|golpista\w*|hacke\w*|invadi\w*|invasao|phishing)\b",
            r"\b(alguem|ninguem)\b.{0,25}\b(usou|fez|realizou|sacou|comprou|transferiu|pagou|gastou|mexeu|reconhece)\b",
            r"\b(tiraram|sumiu|sumiram|limparam|levaram|roubaram) (o )?(meu )?(dinheiro|grana|tudo|saldo)\b",
            r"\b(cobranca|compra|movimentacao|transacao|saque|transferencia|debito|lancamento|pix)s? (estranh\w*|"
            r"suspeit\w*|desconhecid\w*|esquisit\w*|fantasma)\b",
            r"\b(cartao\b.{0,30}\b(nunca saiu|ta comigo|esta comigo)|passaram (meu|o) cartao)\b",
        ],
    },
    "dispute_incorrect_charge_or_fee": {
        "es": [
            r"\b(duplicad\w*|por duplicado|doble (cobro|cargo|compra|debito)|(cobro|cargo|compra) doble|dos veces|"
            r"2 veces|(dos|2) (cargos|cobros|debitos)|(cobr|carg|descont|debit)\w* doble|repetid[ao]s?)\b",
            r"\b(de mas|mas de lo (que|acordado|debido)|en exceso|excesiv\w*|sobrecobro|sobrecargo|"
            r"acordad[oa]|pactad[oa]|no cuadra|no coincide|(corrij\w*|devuelv\w*|reembols\w*) la diferencia)\b",
            r"\b(monto|importe|precio|valor|cantidad|total|cargo|cobro|cambio)\b.{0,50}\b(incorrect\w*|equivocad\w*|"
            r"errone\w*|no es (el )?correct[oa]|distint\w*|diferente|mas alto|superior|mayor|mas caro|carisim[oa]|"
            r"demasiado)\b",
            r"\b(en vez de|en lugar de|cuando (era|eran|debia|debian)|pero (era|eran|costaba|salia|valia))\b",
            rf"\b{_CHARGE_ES}\b.{{0,60}}\b{_FEE_ES}\b",
            rf"\b{_FEE_ES}\b.{{0,60}}\b{_CHARGE_ES}",
            r"\b(indebid\w*|no (me )?corresponde\w*|no deberian? (cobrar\w*|haber|aplicar\w*|existir|estar)|"
            r"sin (motivo|razon)|no tenian? por que|sin anualidad|exent[ao]|"
            r"nunca me (informaron|avisaron))\b",
            r"\b(cajero|atm)\b.{0,60}\b(no (me )?(dio|entrego|salio|solto|dispenso)|nunca (me )?(dio|entrego|salio)|"
            r"sin (darme|entregar))\b",
            r"\bno (me )?(salio|salieron|dio|entrego|solto|recibi) (el |los |la )?(nada|dinero|efectivo|plata|billetes|"
            r"lana|guita)\b",
            r"\b(entrego|dio|solto|salio) (solo |solamente )?(parte|la mitad|menos)\b",
            r"\b(cancele|cancelamos|di de baja|me di de baja|me desuscribi|anule|desafilie|cancelad[ao]|cancelacion|"
            r"(despues de|tras) cancelar)\b.{0,80}\b(cobr\w*|carg\w*|siguen|sigue|todavia|aun)\b",
            r"\b(siguen|sigue|continuan|continua|volvieron a) (cobr|carg|debit|descont)\w*",
            r"\b(no se difirio|diferid\w*|a meses|meses sin intereses|de contado|en una sola cuota|todo junto)\b",
            r"\b(esperando|aguardando) (un |el |mi |o |meu |um )?(pedido|paquete|producto|encomenda)\b",
            r"\b(pedido|producto|paquete|envio|mercancia|mercaderia|servicio)\b.{0,50}\b(no (me )?(llego|ha llegado|"
            r"recibi)|(nunca|jamas) (me )?(llego|recibi)|nada que llega)\b",
            r"\b(no me (han|ha) entregado|nunca (me )?entregaron|no me entregaron)\b",
            r"\b(cobr|carg)\w* (mal|incorrect\w*|equivocad\w*|erroneamente|por error|mas|un (valor|monto) mayor)\b",
        ],
        "pt": [
            r"\b(duplicad\w*|duplicidade|em dobro|dobrad[ao]|duas vezes|2 vezes|2x|(duas|2) (cobrancas|compras)|"
            r"(dois|2) (debitos|lancamentos)|cobranca dupla|compra dupla|repetid[ao]s?)\b",
            r"\b(a mais|a maior|mais do que (o )?(devido|combinado|deveria)|excessiv\w*|combinad[oa]|"
            r"valor (certo|correto|real)|nao bate|nao (ta |esta )?batendo|nao confere)\b",
            r"\b(valor|preco|quantia|total|montante|cobranca)\b.{0,50}\b(errad\w*|incorret\w*|diferente|maior|"
            r"mais alto)\b",
            r"\b(em vez de|ao inves de|no lugar de|quando (era|eram|deveria)|mas (era|eram|custava|saia)|"
            r"paguei menos|era (bem )?(menor|menos))\b",
            rf"\b{_CHARGE_PT}\b.{{0,60}}\b{_FEE_PT}\b",
            rf"\b{_FEE_PT}\b.{{0,60}}\b{_CHARGE_PT}",
            r"\b(indevid\w*|nao (e|era) devid\w*|nao deveria(m)? (cobrar|ter|me|existir|estar)|sem (motivo|razao)|"
            r"nao tinha(m)? que|isent[ao]|sem anuidade|nao fui informad\w*|ninguem me avisou)\b",
            r"\b(caixa|atm|terminal)\b.{0,60}\b(nao (me )?(deu|liberou|entregou|soltou|saiu|pagou)|"
            r"nunca (saiu|liberou)|sem (liberar|entregar|soltar))\b",
            r"\bnao (saiu|sairam|liberou|veio|vieram|recebi) (o |as |os |a )?(nada|dinheiro|notas?|cedulas?|grana)\b",
            r"\b(entregou|deu|soltou|saiu) (so |somente |apenas )?(parte|metade|menos)\b",
            r"\b(engoliu|comeu|reteve) (o |meu )?(dinheiro|grana)\b",
            r"\b(cancelei|cancelamos|cancelad[ao]|cancelamento|desassinei|encerrei|(depois de|apos) cancelar)\b"
            r".{0,80}\b(cobr\w*|debit\w*|continu\w*|ainda)\b",
            r"\b(continuam|continua|continuo|seguem|segue|voltaram a)( sendo)? (cobr|debit|descont)\w*",
            r"\b(parcel\w*|de uma vez|valor cheio|a vista)\b",
            r"\b(pedido|produto|encomenda|mercadoria|servico)\b.{0,50}\b(nao (chegou|foi entregue|recebi|veio)|"
            r"nunca (chegou|recebi|veio))\b",
            r"\b(nao (me )?entregaram|nunca entregaram|nao foi entregue|nunca foi entregue)\b",
            r"\b(cobr|debit)\w* (errad\w*|incorret\w*|por engano|equivocad\w*|a mais)\b",
            r"\b(algo|alguma coisa|coisa) (errad[ao]|mal)\b|\b(ta|esta) errad[ao]\b",
        ],
    },
    "dispute_generic": {
        "es": [
            r"\b(disput\w*|impugn\w*|aclaracion\w*)\b",
            r"\b(problema|resolver|reclamar|reclamo|queja|hablar (de|sobre))\b.{0,30}\b(cargo|cobro|debito|consumo)\b",
        ],
        "pt": [
            r"\bcontestac\w*\b",
            r"\bcontest(ar|o|ei|a)\b.{0,20}\b(compra|cobranca|debito|transacao|lancamento|valor|pix)\b",
            r"\b(problema|resolver|falar sobre|reclamar|reclamacao)\b.{0,30}\b(cobranca|compra|debito|lancamento)\b",
        ],
    },
    "prompt_injection": {
        "es": [
            r"\b(ignora|ignore|ignoren|ignorar|olvida|olvidate de|omite|descarta)( (todas|todo|las|tus|sus|los|lo|"
            r"cualquier|tu|esas|estas))* (instrucciones|reglas|indicaciones|politicas|restricciones|anteriores|"
            r"previas|lo anterior|programacion)\b",
            r"\b(pausa|desactiva|apaga|suspende|saltea|salta|saltate|omiti|omite)( en pausa)? (tus |las |sus |los )?"
            r"(reglas|validacion\w*|verificacion\w*|controles|restricciones|politicas)\b",
            r"\b((instrucciones|reglas) (anteriores|previas|del sistema|internas)|instruccion (prioritaria|del sistema|"
            r"nueva|interna)|nueva instruccion|regla interna)\b",
            r"\b(ahora eres|ahora sos|desde ahora|a partir de ahora (eres|seras|actua)|actua como|haz de cuenta que|"
            r"finge (que|ser)|responde (solo|unicamente) con|sin restricciones)\b",
            r"\bmodo (desarrollador|administrador|admin|debug|dios|sin restricciones|developer)\b",
            r"\b(revela|muestra|muestrame|dime) (tu|tus|el|las) (prompt|instrucciones|reglas|configuracion)\b",
            r"\bsin (pedir |hacer )?(verificar|validar|preguntar|revisar|analisis|verificacion|validacion|"
            r"autenticacion|otp|codigo|token)\b",
            r"\b(system|prompt|jailbreak|override|admin mode|developer mode|para el bot|al bot)\b",
            r"\b(sistema)\b.{0,10}\b(marca|aprueba|desactiva|ignora|ejecuta)\b",
            r"\b(ignore (all|any|the|previous|prior)|disregard|you are now|act as)\b",
        ],
        "pt": [
            r"\b(ignore|ignora|ignorar|esqueca|esquece|desconsidere|desconsidera|descarte)( (todas|todo|as|suas|"
            r"tuas|os|qualquer|o|essas|estas))* (instrucoes|regras|orientacoes|politicas|restricoes|anteriores|"
            r"acima|programacao)\b",
            r"\b(pause|pausa|desative|desativa|desliga|desligue|pule|pula|pular|ignore|ignora)( a| o| as| os| suas| "
            r"seus)? (regras|validac\w*|verificac\w*|confirmac\w*|controles|restricoes)\b",
            r"\b((instrucoes|regras) (anteriores|do sistema|internas)|instrucao (prioritaria|para o robo|do sistema|"
            r"interna)|nova instrucao|regra interna)\b",
            r"\b(agora voce e|a partir de agora (voce|seja|aja)|aja como|finja (que|ser)|responda (so|somente|"
            r"apenas) com|sem restricoes)\b",
            r"\bmodo (desenvolvedor|administrador|admin|debug|irrestrito|deus)\b",
            r"\b(revele|mostre|mostra|me diga) (seu|suas|o|as) (prompt|instrucoes|regras|configuracao)\b",
            r"\bsem (pedir |fazer )?(verificar|validar|perguntar|conferir|analise|verificacao|validacao|"
            r"autenticacao|otp|codigo|token)\b",
            r"\b(sistema)\b.{0,10}\b(marque|aprove|desative|ignore|finalize|execute)\b",
        ],
    },
    "refund_request": {
        "es": [r"\b(reembols\w*|devolucion\w*|reintegr\w*|contracargo|nota de credito|devolvi|devolvimos)\b"],
        "pt": [r"\b(estorn\w*|reembols\w*|devolucao|devolvi|devolvemos|ressarc\w*|chargeback|(dinheiro|diferenca) "
               r"de volta)\b"],
    },
    "other_complaint": {
        "es": [
            r"\b(asesor\w*|ejecutiv[ao]s?|emplead[ao]s?|funcionari[ao]s?|operador\w*|call center|linea de atencion|"
            r"servicio al cliente|atencion al cliente)\b",
            r"\b(grosero|grosera|groseros|maleducad\w*|mal educad\w*|maltrat\w*|descortes|prepotente|acos\w*|"
            r"(trato|trataron) (muy )?mal|me (grito|gritaron|cortaron|colgaron|colgo|ignoraron|humillaron)|"
            r"mal(a)? (atencion|servicio|trato)|pesim[ao] (atencion|servicio|trato)|falta de respeto)\b",
            r"\b(fila|cola|tiempo de espera|en espera|esperando|espere)\b.{0,40}\b(hora|horas|minutos|manana|eterno|"
            r"eternidad)\b",
            r"\b(hora|horas|minutos)\b.{0,30}\b(fila|cola|esperando|espera|en (la )?linea|al telefono)\b",
            r"\b(nadie|ninguno)\b.{0,20}\b(resuelve|contest\w*|atiend\w*|atendio|respond\w*|soluciona|ayuda|"
            r"(da|ha dado) seguimiento)\b",
            r"\b(sin respuesta|sin solucion|seguimiento|me (derivan|pasan|transfieren) (a|de)|imposible comunicarse|"
            r"no (puedo|logro) comunicarme)\b",
            rf"\b{_APP}\b.{{0,40}}\b{_FAIL_ES}",
            rf"\b{_FAIL_ES}.{{0,40}}\b{_APP}\b",
            r"\b(cajero|cajeros|atm)\b.{0,40}\b(no (sirve|funciona|anda)|danad\w*|fuera de servicio|descompuest\w*|"
            r"sin (dinero|efectivo))\b",
            r"\b(codigo|clave|token|sms|otp)s?\b.{0,40}\b(no (me )?llegan?|nunca (me )?llegan?|falla\w*)\b|"
            r"\bno me llegan? (los |el |las |la )?(codigo|clave|token|sms|mensaje)",
            r"\b(llamadas?|mensajes|correos|sms|whatsapps?)\b.{0,30}\b(publicidad|promocion\w*|ofertas?|ofrec\w*|spam|"
            r"marketing|telemarketing|cobranza)\b",
            r"\b(me (llaman|llamaron|siguen llamando|marcan|escriben|mandan)|no me (llamen|escriban))\b.{0,40}"
            r"\b(ofrec\w*|promocion\w*|vender|publicidad|ofertas?|todos los dias|a cada rato|insist\w*)\b",
            r"\b(spam|telemarketing|publicidad|dejen de (llamar|llamarme|escribirme|mandarme))\b",
            r"\b(queja|quejarme|me quejo|me quiero quejar|reclamo formal|pesim[ao]s?|malisim[ao]|horrible|terrible|"
            r"desastre|verguenza|una burla|indignad[ao]|hart[ao]s?|inaceptable|insatisf\w*|frustrante|condusef|"
            r"superfinanciera|defensa del consumidor)\b",
        ],
        "pt": [
            r"\b(atendente\w*|gerente|funcionari[ao]s?|operador\w*|central de atendimento|sac|ouvidoria|call center|"
            r"atendimento)\b",
            r"\b(grosseir\w*|mal educad\w*|maltrat\w*|estupid\w*|desrespeit\w*|falta de respeito|me (trataram|tratou) "
            r"mal|mal atendid\w*|descaso|me (ignoraram|ignorou|desligaram|desligou)|desligou na (minha )?cara)\b",
            r"\b(fila|espera|esperando|esperei|aguardando)\b.{0,40}\b(hora|horas|minutos|manha|tarde inteira|"
            r"eternidade|gigante|enorme|imensa)\b",
            r"\b(hora|horas|minutos)\b.{0,30}\b(fila|espera|esperando|aguardando|na linha|no telefone)\b",
            r"\bninguem\b.{0,20}\b(resolv\w*|atend\w*|respond\w*|ajud\w*|retorn\w*)\b",
            r"\b(sem resposta|sem solucao|protocolos?|me (passam|transferem) de|impossivel falar|nao consigo falar)\b",
            rf"\b{_APP}\b.{{0,40}}\b{_FAIL_PT}",
            rf"\b{_FAIL_PT}.{{0,40}}\b{_APP}\b",
            r"\b(caixa eletronico|caixas eletronicos|caixa 24|atm)\b.{0,40}\b(nao funciona|quebrad\w*|fora de servico|"
            r"sem dinheiro|estragad\w*)\b",
            r"\b(codigo|token|sms|otp)s?\b.{0,40}\b(nao (me )?chegam?|nunca chegam?|falha\w*)\b",
            r"\b(ligac(ao|oes)|mensage(m|ns)|sms|e ?mails?)\b.{0,30}\b(oferec\w*|oferta\w*|promoc\w*|propaganda|"
            r"vender|marketing|cobranca)\b",
            r"\b(me (ligam|ligaram|ligando|liga|mandam)|nao me liguem)\b.{0,40}\b(oferec\w*|oferta\w*|vender|"
            r"propaganda|todo dia|toda hora)\b",
            r"\b(propaganda|spam|telemarketing|parem de (me )?(ligar|mandar))\b",
            r"\b(reclamacao|quero reclamar|vou reclamar|reclame aqui|procon|pessim[ao]s?|horrivel|terrivel|absurdo|"
            r"vergonha|lamentavel|indignad[ao]|insatisf\w*|inaceitavel|cansad[ao] de)\b",
        ],
    },
    "out_of_scope": {
        "es": [
            r"\b(pedir|solicitar|sacar|tramitar|obtener|necesito|quiero|quisiera|me (dan|darian|prestan|prestarian|"
            r"aprueban|aprobarian|otorgan)|puedo (pedir|sacar|tener|obtener|acceder)|califico|requisitos|tasa|"
            r"simul\w*|cotiz\w*|ofrecen)\b.{0,40}\b(prestamo|(?<!tarjeta de )credito|hipoteca|financiamiento|"
            r"adelanto de (nomina|sueldo))",
            r"\b(aument\w*|subir|suban|incrementar|ampliar|ampliacion|mas)\b.{0,30}\b(limite|cupo|linea de credito)\b",
            r"\b(invertir|inversion\w*|cdt|cdts|plazo fijo|cetes|fondos? de inversion|bolsa de valores|cripto\w*|"
            r"bitcoin|comprar dolares|dolares mep|acciones de)\b",
            r"\b(abrir|aperturar|apertura de|crear)\b.{0,20}\b(cuenta|caja de ahorro|tarjeta)\b",
            r"\b(solicitar|pedir|tramitar|quiero|quisiera) (una|un) (nueva |nuevo )?(tarjeta|cuenta)\b",
            r"\b(cambiar|actualizar|modificar|corregir|cambio de|actualizacion de)\b.{0,25}\b(direccion|domicilio|"
            r"telefono|celular|correo|email|mail|nombre|datos|contrasena|clave|pin|nip|usuario)\b",
            r"\b(olvide|recuperar|restablecer|resetear) (mi |la |el )?(contrasena|clave|pin|nip|usuario)\b",
            r"\b(contratar|cotizar|comprar)\b.{0,20}\b(seguro|poliza)\b|\bseguro\b.{0,40}\b(contrat\w*|cuesta|cubre)\b",
            r"\b(a que hora|horario\w*|donde (hay|queda|esta))\b.{0,30}"
            r"\b(sucursal\w*|oficina\w*|agencia\w*|cajero\w*)\b",
            r"\b(chiste|clima|pronostico|receta|futbol|partido de|pelicula|cancion|poema|horoscopo|capital de|"
            r"quien gano)\b",
        ],
        "pt": [
            r"\b(pedir|solicitar|contratar|tirar|pegar|fazer|preciso de|quero|queria|gostaria de|consigo|posso|"
            r"liberam|aprovam|simul\w*|taxa|quanto)\b.{0,40}\b(emprestimo|financiamento|consignado|credito pessoal|"
            r"credito imobiliario|hipoteca)\b",
            r"\b(aument\w*|subir|ampliar|mais)\b.{0,30}\b(limite)\b",
            r"\b(investir|investimento\w*|cdb|lci|lca|tesouro direto|bolsa de valores|cripto\w*|bitcoin|"
            r"fundos? de investimento|renda fixa|acoes da)\b",
            r"\b(abrir|criar)\b.{0,20}\b(conta|cartao)\b",
            r"\b(pedir|solicitar|quero|queria) (um|uma) (novo |nova )?(cartao|conta)\b",
            r"\b(mudar|alterar|atualizar|trocar|corrigir|mudanca de|atualizacao)\b.{0,25}\b(endereco|telefone|celular|"
            r"email|e mail|nome|cadastro|dados|senha|pin|usuario)\b",
            r"\b(esqueci|recuperar|redefinir|resetar) (a |minha |meu |o )?(senha|pin|usuario)\b",
            r"\b(contratar|cotar|fazer)\b.{0,20}\b(seguro|apolice)\b|\bseguro\b.{0,40}\b(contrat\w*|funciona|custa|"
            r"cobre)\b",
            r"\b(que horas|horario\w*|onde (fica|tem))\b.{0,30}\b(agencia\w*|caixa\w*)\b",
            r"\b(piada|previsao do tempo|receita de|futebol|jogo do|filme|novela|musica|poema|horoscopo|capital d[aeo]|"
            r"quem ganhou)\b",
        ],
    },
    "account_payment_inquiry": {
        "es": [
            r"\b(saldo|cuanto (dinero|plata|lana|tengo|me queda|debo|hay|me toca)|disponible|"
            r"cual es mi (limite|cupo))\b",
            r"\b(movimientos|transacciones|ultimos (movimientos|cargos|consumos|compras|pagos)|estado de cuenta|"
            r"extracto|resumen|historial|consumos|corte)\b",
            r"\b(rechaz\w*|declin\w*|denegad\w*|fondos insuficientes|no fue aprobad\w*|no (la |lo )?aprob\w*)\b",
            r"\bno (me )?(paso|pasa|pasaba|dejo|deja)\b.{0,20}\b(compra|pago|tarjeta|transferencia)\b",
            r"\bno se (pudo|puede) (pagar|comprar|completar|procesar)\b",
            rf"\b{_PAYOBJ_ES}\b.{{0,60}}\b(pendiente|en proceso|procesando|refleja\w*|acredit\w*|aplicad\w*|"
            rf"registrad\w*|no (aparece|aparecio|figura|llega|llego|ha llegado|cae|cayo)|cuando (llega|cae))\b",
            rf"\b(pendiente|en proceso|refleja\w*|acredit\w*|no (aparece|aparecio|veo|llega|llego|ha llegado))\b"
            rf".{{0,50}}\b{_PAYOBJ_ES}\b",
            r"\b(quedo|esta|fue) (registrad|aplicad|acreditad|procesad)\w*\b",
            r"\b(fecha (de corte|de pago|limite|de vencimiento)|pago minimo|vencimiento|vence|cuando (tengo que|debo) "
            r"pagar|cuanto (tengo que|debo) pagar)\b",
            r"\b(que es|de que es|a que (corresponde|se debe)|que significa|por que (me )?(aparece|sale|hay)|"
            r"explic\w*|no entiendo)\b.{0,40}\b(cargo|cobro|movimiento|debito|compra|concepto|transaccion|descuento)\b",
            r"\b(cargo|cobro|movimiento|debito|compra|consumo)\b.{0,60}"
            r"\b(que es|que significa|de donde (salio|viene))\b",
            r"\b(consult\w*|revis\w*|verific\w*|confirm\w*|saber si|checar|chequear)\b.{0,30}\b(pago|transferencia|"
            r"deposito|cuenta|tarjeta|cargo|compra|movimiento)\b",
            r"\b(comprobante|constancia de pago|numero de operacion)\b",
        ],
        "pt": [
            r"\b(saldo|quanto (eu )?(tenho|sobrou|resta|falta|devo|deu|ficou)|de quanto (veio|e|ficou|foi)|disponivel|"
            r"qual (e )?(o )?meu limite)\b",
            r"\b(extrato|movimentacoes|transacoes|lancamentos|ultimas (compras|transacoes|movimentacoes)|historico|"
            r"fatura)\b",
            r"\b(recusad\w*|recusou|negad\w*|nao (foi )?aprovad\w*|nao passou|nao passa|saldo insuficiente)\b",
            r"\bnao (consegui|consigo) (pagar|comprar|passar|transferir|fazer o pix)\b",
            rf"\b{_PAYOBJ_PT}\b.{{0,60}}\b(pendente|em (processamento|analise)|processando|nao (caiu|chegou|apareceu|"
            rf"compensou|entrou)|ja (caiu|compensou|entrou)|compensad\w*|creditad\w*|quando (cai|vai cair|compensa|"
            rf"entra|chega)|ficou pres[ao])\b",
            rf"\b(pendente|nao (caiu|chegou|apareceu|compensou|entrou))\b.{{0,50}}\b{_PAYOBJ_PT}\b",
            r"\b(vencimento|vence|data de (corte|pagamento|fechamento)|fechamento da fatura|pagamento minimo|"
            r"valor minimo|quanto (devo|tenho que pagar))\b",
            r"\b(o que e|do que e|a que se refere|referente a que|o que significa|por que (aparece|tem|veio)|"
            r"nao entendi|explica\w*)\b.{0,40}\b(cobranca|compra|debito|lancamento|transacao|movimentacao|desconto|"
            r"valor)\b",
            r"\b(cobranca|compra|debito|lancamento|transacao|taxa|tarifa|valor)\b.{0,60}"
            r"\b(o que e|do que e|o que significa|de onde (veio|saiu))\b",
            r"\b(consult\w*|revis\w*|verific\w*|confer\w*|checar|confirmar|saber se)\b.{0,30}"
            r"\b(pagamento|transferencia|pix|deposito|conta|cartao|cobranca|compra|boleto|lancamento)\b",
            r"\b(comprovante)\b",
        ],
    },
    # Last resort before the default: any banking object or charge, which the convention treats as an inquiry.
    "charge_mention": {
        "es": [
            r"\b(cargos?|cobros?|cobraron|cobran|cobrado|debitaron|descontaron|compras?|pagos?|transferencias?|"
            r"retiros?|depositos?|tarjetas?|cuenta|movimiento|transaccion)\b",
        ],
        "pt": [
            r"\b(cobrancas?|cobraram|cobrado|debitaram|descontaram|compras?|pagamentos?|transferencias?|saques?|pix|"
            r"depositos?|cartao|conta|movimentacao|transacao|debito)\b",
        ],
    },
}

RESOLUTION_ORDER = (
    ("other_customer_data", "out_of_scope"),
    ("social_engineering", "out_of_scope"),
    ("card_lost_or_block", "card_lost_or_block"),
    ("dispute_unrecognized_charge", "dispute_unrecognized_charge"),
    ("dispute_incorrect_charge_or_fee", "dispute_incorrect_charge_or_fee"),
    ("dispute_generic", "dispute_unrecognized_charge"),
    ("prompt_injection", "out_of_scope"),
    ("refund_request", "dispute_incorrect_charge_or_fee"),
    ("other_complaint", "other_complaint"),
    ("out_of_scope", "out_of_scope"),
    ("account_payment_inquiry", "account_payment_inquiry"),
    ("charge_mention", "account_payment_inquiry"),
)

_COMPILED = {group: [re.compile(p) for lang in ("es", "pt") for p in GLOSSARY[group][lang]] for group in GLOSSARY}
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize(text):
    """Lowercase, strip accents, turn punctuation into spaces, collapse whitespace and expand chat abbreviations."""
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    words = _NON_WORD.sub(" ", text).split()
    return " ".join(ABBREVIATIONS.get(w, w) for w in words)


def matches(text):
    """Rule groups that fire on a message, with the patterns that matched, in resolution order."""
    norm = normalize(text)
    fired = {}
    for group, _ in RESOLUTION_ORDER:
        hits = [rx.pattern for rx in _COMPILED[group] if rx.search(norm)]
        if hits:
            fired[group] = hits
    return fired


def predict(text):
    """One intent for a message: the class of the first rule group that matches, else DEFAULT_INTENT."""
    norm = normalize(text)
    if norm:
        for group, intent in RESOLUTION_ORDER:
            if any(rx.search(norm) for rx in _COMPILED[group]):
                return intent
    return DEFAULT_INTENT


def predict_many(texts):
    return [predict(t) for t in texts]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Route a message with the keyword baseline and show the rules that fired.")
    ap.add_argument("text", nargs="+")
    args = ap.parse_args(argv)
    text = " ".join(args.text)
    print(predict(text))
    for group, hits in matches(text).items():
        print(f"  {group}: {len(hits)} pattern(s)")


if __name__ == "__main__":
    main()
