# Recomendaciones desde campo

Cualquier persona de la empresa que vea en una visita que un negocio necesita
otro servicio lo recomienda desde **`/recomendar/`**. No necesita usuario de
CRM. La recomendación entra al pipe como un lead normal con origen
**Referido operaciones**.

## El link

Es uno solo para todos y no pide login. Dirección lo copia o lo comparte por
WhatsApp desde **Técnicos** (`/tecnicos/`, en el menú).

Como es abierto:
- **No muestra la lista de clientes.** Quien recomienda escribe el nombre y
  el servidor lo empata con CS sin revelar nada. La confirmación repite lo
  que escribió, no el nombre registrado.
- **Tiene protección básica contra abuso:** un límite de 20 envíos por hora
  por IP y un campo trampa para bots.

## Quién recomendó y quién cobra

El formulario pide **nombre y apellido** (obligatorio) y **celular**
(opcional). Los dos se guardan en el lead (`referido_nombre`,
`referido_telefono`) y el vendedor los ve.

Cobra quien esté registrado en Técnicos con su celular:
- Si el celular coincide con alguien registrado al recomendar, el lead queda
  ligado a esa persona (`referido_por_id`). La etapa *prospectar* del
  tabulador se le atribuye de forma manual, para que el avance del vendedor
  no se la quite.
- Si no está registrado, **Técnicos** lo muestra en «Recomendaron sin estar
  registrados». Al registrarlo, sus recomendaciones anteriores con ese
  celular pasan a su nombre. Una venta ya cerrada no se toca, porque su
  reparto ya quedó congelado.
- Si no dio celular, no se puede acreditar sola. Dirección lo registra y le
  asigna la etapa en la comisión del lead.

Los registrados son perfiles de `usuarios` con rol comercial «Técnico»,
`en_turno = false` y sin marcas. Nunca entran al reparto de leads ni salen en
las listas de vendedores, pero sí cobran en el tabulador.

## Qué pasa al enviar

1. **Empresa.** Quien recomienda contesta si el negocio ya es cliente de
   Grupo Avantex.
   - **Sí:** se busca en CS por nombre exacto (sin importar mayúsculas ni
     espacios) o, si no hay, un único cliente que lo contenga. Con dos o más
     candidatos no se adivina. El lead se liga a la empresa comercial, y si
     no existe se crea marcada como cliente. Si no aparece en CS, se registra
     igual como cliente.
   - **No:** es prospecto. No se liga a CS ni se crea empresa.

   Se ofrecen Aromatex, Pestex, Weldex y Café Origen. Nexo no, porque no se
   detecta en una visita.
2. **Vendedor.** Es el primero que aplique de esta lista:
   1. el ejecutivo comercial de la cuenta en CS, si vende esa unidad;
   2. el dueño de la empresa en comercial, si está en turno y la vende;
   3. el reparto normal (marca, zona, carga).

   Si nadie vende esa unidad, el lead queda sin asignar.
3. **Aviso.** El vendedor recibe el correo de asignación de siempre, con una
   línea «Recomendado por». Lo que vio la persona y su celular van en las
   notas del lead, no en el correo.
4. **Comisión.** El lead se reparte por el tabulador aunque el vendedor no
   tenga perfil multi-UN. El tipo de comisión propuesto es *lead otorgado*.
   Dirección puede cambiar el tipo y la atribución en el cierre, como en
   cualquier lead.
5. **CS.** Solo si la cuenta es AAA se abre además una oportunidad en el pipe
   de CS para su KAM. En las demás cuentas, CS no interviene.

Si llega otra recomendación de la misma empresa y unidad mientras el lead
sigue abierto, no se crea otro lead: lo nuevo se agrega a sus notas.

## Despliegue

La migración `f0a3c19d7b52` agrega las columnas que `Lead` lee en cada
consulta. **Hay que aplicarla antes de desplegar el código.** Sin ella, se cae
el pipe completo.

Café Origen ya es unidad válida, pero hay que capturar su tasa en
`/comisiones/` y dar la marca a los vendedores que la atiendan. Sin eso, sus
leads quedan sin asignar y el cierre no calcula comisión.

## Pruebas

`python3 -m pytest -q tests/test_referidos.py`
