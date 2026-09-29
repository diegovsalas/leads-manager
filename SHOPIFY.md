# Leads de Shopify (tienda Weldu) al CRM

La tienda **Weldu** (`www.welduapp.com`) manda sus prospectos al CRM como leads
de la unidad **Weldex**, que los reparte entre sus vendedores por el mismo
round-robin que usan Meta y LinkedIn.

Hay dos caminos, y se necesitan los dos porque Shopify no cubre todo:

| Camino | Qué cubre | Cómo viaja |
|---|---|---|
| Webhooks | alta de cliente, boletín, carrito abandonado, compra | servidor a servidor, firmado |
| Endpoint del formulario | el formulario de contacto del tema | el navegador del visitante |

---

## 1. Variables de entorno en Render

```
SHOPIFY_WEBHOOK_SECRET   ← lo da Shopify al crear el webhook
SHOPIFY_SHOP_DOMAIN      www.welduapp.com
SHOPIFY_MARCA            Weldex
SHOPIFY_FORM_ORIGINS     https://www.welduapp.com,https://welduapp.com
```

Sin `SHOPIFY_WEBHOOK_SECRET` el endpoint responde **503** a propósito: es
preferible rechazar a tragarse peticiones sin poder comprobar de dónde vienen.

## 2. Webhooks en Shopify

**Configuración → Notificaciones → Webhooks**, formato **JSON**, todos apuntando
a la misma URL:

```
https://leads-manager-avantex.onrender.com/webhook/shopify
```

| Evento | Qué hace en el CRM |
|---|---|
| `customers/create` | crea el lead |
| `customers/update` | rellena datos que falten, sin pisar lo que el vendedor haya corregido |
| `checkouts/create` y `checkouts/update` | lead de carrito abandonado, con el monto en las notas |
| `orders/create` | cierra el lead como ganado y registra la venta |

Al crear el primero, Shopify muestra la **clave secreta**. Esa es la que va en
`SHOPIFY_WEBHOOK_SECRET`. Es la misma para todos los webhooks de la tienda.

## 3. Formulario de contacto del tema

El formulario nativo de Shopify **no dispara ningún webhook**, así que se publica
por JavaScript. Va en el Liquid del tema, en la plantilla del formulario:

```html
<!-- Campo trampa: invisible para una persona, irresistible para un bot -->
<input type="text" name="website" tabindex="-1" autocomplete="off"
       style="position:absolute;left:-9999px" aria-hidden="true">

<script>
document.querySelector('form[action*="/contact"]')?.addEventListener('submit', function () {
  const d = new FormData(this);
  fetch('https://leads-manager-avantex.onrender.com/webhook/shopify/form', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    keepalive: true,   // el envío sobrevive a que la página navegue
    body: JSON.stringify({
      nombre:   d.get('contact[Name]'),
      email:    d.get('contact[email]'),
      telefono: d.get('contact[phone]'),
      mensaje:  d.get('contact[body]'),
      website:  d.get('website'),
    }),
  }).catch(() => {});   // si el CRM falla, el formulario de Shopify sigue su curso
});
</script>
```

**Por qué este endpoint no lleva API key.** El código del tema es código fuente
público: cualquiera puede leerlo. Una llave ahí serviría para crear leads a
voluntad y para usar el resto de la API. En su lugar el endpoint está abierto
pero acotado: límite de 10 envíos por hora por IP, campo trampa, y solo acepta
peticiones desde los dominios de `SHOPIFY_FORM_ORIGINS`.

## 4. Otros formularios web (no solo Weldu)

El mismo endpoint atiende cualquier formulario, no solo el de Shopify. Se llega
por `/webhook/form` — `/webhook/shopify/form` sigue funcionando y es un alias.

**La unidad la manda el formulario**, en un campo `marca`:

```json
{ "nombre": "...", "email": "...", "whatsapp": "...", "marca": "Aromatex", ... }
```

Sin ese campo entra como **Weldex**, que es lo correcto para Weldu. Una marca
que no exista se ignora y cae al mismo valor por omisión, para que un dato
cualquiera no invente unidades.

### Campos que no son nombre, correo ni teléfono

Tres tienen columna propia:

| Del formulario | Columna |
|---|---|
| `empresa` | `empresa_nombre` |
| `industria` | `tipo_industria` |
| `whatsapp` o `telefono` | `telefono` |

**Todo lo demás se vuelca en las notas del lead**, con su etiqueta, para que el
vendedor lo vea al abrir la tarjeta. Es genérico: si mañana agregan una
pregunta, aparece sola en las notas en vez de perderse en silencio.

Ejemplo con el cuestionario de aromas de Aromatex (25 campos, 5 con columna):

```
Formulario web (Aromatex).

Respuestas del formulario:
  Arquetipo: El Refugio
  Compatibilidad: 87%
  Intensidad recomendada: Media-baja
  Aroma 1: Vainilla tostada
  ...
```

El resultado del diagnóstico va arriba a propósito: es lo que el vendedor
necesita en los primeros cinco segundos. Las respuestas que lo justifican van
después. `payload_json` y `fecha` se omiten — el primero duplica lo que ya está
desglosado y la segunda la pone el CRM.

---

## Qué entra y qué no

Un correo suelto **no** crea lead. La tienda da de alta un cliente por cada
suscripción al boletín y la mayoría llega sin nombre ni teléfono; meterlos todos
sería ruido para los dos vendedores de Weldex. Se exige **nombre o teléfono**.

También se descartan los correos de dominios desechables — ya hay basura de ese
tipo dada de alta en la tienda.

Nada se duplica: se busca primero por el id de Shopify, luego por teléfono y por
correo. El mismo cliente puede llegar por varios eventos (abandona un carrito,
se registra, compra) y todos caen en la misma tarjeta.

## Órdenes y comisiones

`orders/create` cierra el lead como ganado **por el monto de la orden**, y crea
la venta llamando al mismo código que usa la pantalla de cierre — no hay una
segunda matemática de comisiones que pueda desviarse.

Se clasifica como **servicio único / eventual**, porque el catálogo de Weldu son
visitas de diagnóstico: compras de una sola vez, no suscripciones.

**Hay algo que decidir aquí, y conviene revisarlo con el tiempo.** Los productos
de la tienda son diagnósticos de $580. Si el trabajo real se cotiza después y
vale más, la venta registrada se queda en los $580 y la comisión se calcula
sobre eso. Si eso empieza a pasar seguido, el cierre automático debería
sustituirse por una nota y un cierre a mano con el monto real.

## Comprobar que funciona

En Shopify, cada webhook tiene **"Enviar prueba"**. Después:

- El lead aparece en el pipeline con origen **Web** y marca **Weldex**.
- En los logs de Render sale `[shopify] topic=... id=...`.
- Firma inválida responde **401**; un evento que no manejamos, **200 ignorado**.

El endpoint siempre responde 200 salvo que la firma falle. Es a propósito: un
500 hace que Shopify reintente 19 veces y termine desactivando el webhook.
