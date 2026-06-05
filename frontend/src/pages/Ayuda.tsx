/**
 * Guía para vendedores. Usa "mock UI elements" inline (chips, botones)
 * en vez de capturas de pantalla — así se mantiene en sync con la app real.
 */

export default function Ayuda() {
  return (
    <div className="max-w-3xl">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Guía para vendedores</h1>
        <p className="text-sm text-gray-500 mt-0.5">Cómo cargar clientes, leads y oportunidades en el CRM</p>
      </div>

      {/* Los 4 conceptos */}
      <Card title="Los 4 conceptos que tenés que tener claros">
        <ConceptRow
          icon="🏢"
          name="Empresa"
          example='"Tec Milenio", "Walmart", "Coca-Cola"'
          desc="Una sola ficha por compañía. Es la entidad que existe en el mundo."
        />
        <ConceptRow
          icon="👤"
          name="Contacto"
          example='"Lic. Pérez, Gerente de Compras de Tec Milenio"'
          desc="Cada persona con la que tratás. Una empresa puede tener varios contactos."
        />
        <ConceptRow
          icon="📨"
          name="Lead"
          example="Mensaje entrante por WhatsApp o anuncio"
          desc={
            <>
              Lo crea <b>automáticamente el bot</b> cuando alguien escribe al WhatsApp o llega
              por una campaña. <b>Vos no creás leads a mano.</b> Tu trabajo es calificarlos y, si
              valen la pena, convertirlos en Oportunidad.
            </>
          }
        />
        <ConceptRow
          icon="💰"
          name="Oportunidad"
          example='"Tec Milenio - Aromatex 12 campus"'
          desc="La venta concreta que estás trabajando. Es la tarjeta del kanban. La creás vos cuando hay algo real para vender (valor, sucursales, fecha)."
        />
      </Card>

      {/* Árbol de decisión */}
      <Card title='¿Por dónde empiezo? "Me interesa empezar a trabajar a una empresa"'>
        <DecisionBranch
          letter="A"
          color="purple"
          title="Caso outbound — vos prospectaste"
          subtitle="(LinkedIn, llamada en frío, referido, evento)"
          warning="No crees Lead. Lead es solo para entrada automática del bot."
          steps={[
            <>Andá a <b>Empresas</b> en el menú lateral. Buscá la empresa por nombre/RFC. ¿Aparece? Cliqueala. ¿No? Usá <Button>+ Nueva empresa</Button>.</>,
            <>Una vez en la página de la empresa, agregá el contacto con <Button>+ Agregar</Button> en la sección Contactos.</>,
            <>En la sección Oportunidades de la misma página, tocá <Button>+ Agregar</Button> y completá: valor estimado, marca de interés (Aromatex/Pestex/Weldex), número de sucursales y fecha estimada de cierre.</>,
            <>La oportunidad aparece automáticamente en el kanban en etapa <Chip color="yellow">Calificación</Chip>. De ahí la vas moviendo.</>,
          ]}
        />

        <DecisionBranch
          letter="B"
          color="blue"
          title="Caso inbound — alguien te escribió por WhatsApp o vino de un anuncio"
          subtitle=""
          warning="El Lead ya existe (lo creó el bot con el teléfono). No crees uno nuevo."
          steps={[
            <>Andá a <b>Leads</b> en el menú lateral. Encontrá el Lead correcto en la tabla y cliquealo para abrir el modal.</>,
            <>En el modal, en el campo <b>Empresa</b>, buscala por nombre. Si existe → seleccionala. Si no → tocá <Button>+ Crear nueva: "..."</Button> desde el mismo buscador.</>,
            <>Elegí el <b>contacto</b> de esa empresa. Si todavía no existe, tenés que ir a la página de la empresa primero, agregarlo ahí, y volver al Lead. Luego tocá <Button>Guardar vínculos</Button>.</>,
            <>Si el Lead es serio: tocá <Chip color="green">→ Convertir a Oportunidad</Chip>. Eso crea un deal en etapa Calificación ligado a este Lead.</>,
            <>Si no es serio: cambiá la etapa del Lead a <Chip color="red">Perdido</Chip> o dejalo en nurturing desde el kanban.</>,
          ]}
        />

        <DecisionBranch
          letter="C"
          color="green"
          title="Caso cliente existente — la empresa ya te compró y querés venderle algo más"
          subtitle="(upsell, sucursal nueva, producto adicional)"
          warning=""
          steps={[
            <>Andá a <b>Empresas</b> y cliqueá la empresa (ya existe).</>,
            <>En la sección Oportunidades tocá <Button>+ Agregar</Button> — upsell, nueva sucursal, nuevo producto, lo que sea.</>,
            <>Reutilizá los contactos que ya están cargados ahí — no crees duplicados.</>,
          ]}
        />
      </Card>

      {/* Reglas */}
      <Card title="Reglas que no se rompen" tone="red">
        <Rule>
          <b>No crees una empresa dos veces.</b> Antes de crear, buscá. El sistema deduplica por nombre y RFC, pero ayudalo: revisá si aparece algo parecido (Tec Milenio = Tecmilenio = ITESM-Milenio).
        </Rule>
        <Rule>
          <b>No crees un Contacto suelto sin empresa</b> si la empresa existe. Asociá siempre.
        </Rule>
        <Rule>
          <b>No uses el campo "empresa" de la Oportunidad como texto libre.</b> Ligala al Account (la empresa real). El texto libre es legado y se va a deprecar.
        </Rule>
        <Rule>
          <b>No abras una Oportunidad sin marca de interés</b> (Aromatex/Pestex/Weldex). Es lo que después permite reportar por línea de negocio.
        </Rule>
        <Rule>
          <b>No muevas una Oportunidad a <Chip color="green">Cerrado Ganado</Chip> sin valor.</b> Si no hay monto, no hay venta para reportar.
        </Rule>
      </Card>

      {/* Resumen visual */}
      <Card title="Resumen visual">
        <pre className="text-xs text-gray-600 font-mono bg-gray-50 rounded-lg p-4 overflow-x-auto leading-relaxed">
{`                  ¿Cómo llegó el contacto?
                          │
        ┌─────────────────┴─────────────────┐
        ▼                                   ▼
   OUTBOUND (yo)                  INBOUND (WhatsApp/Ads)
        │                                   │
  Empresa → Contacto                  Lead (ya existe)
        ↓                                   │
   OPORTUNIDAD                       Ligar a Empresa + Contacto
   (al kanban)                              │
                                      ¿Califica?
                                       /         \\
                                      sí          no
                                      ↓           ↓
                                 OPORTUNIDAD   Perdido /
                                 (al kanban)   Nurturing`}
        </pre>
      </Card>

      {/* FAQ */}
      <Card title="¿Y si tengo dudas?">
        <Faq q="¿Creo la empresa o no?">
          Buscá primero. Si no estás 100% seguro de que existe, creala — el sistema deduplica por nombre y RFC.
        </Faq>
        <Faq q="¿Es Lead u Oportunidad?">
          ¿Lo creaste vos o entró por el bot? <b>Vos = Oportunidad.</b> <b>Bot = Lead.</b>
        </Faq>
        <Faq q="No sé el valor exacto de la Oportunidad">
          Poné el mejor estimado posible. Mejor un número aproximado que cero. Lo podés actualizar después.
        </Faq>
      </Card>

      <p className="text-xs text-gray-400 mt-6 italic">
        ¿Algo confuso, falta un caso, o un botón cambió de lugar? Avisale al admin del CRM.
      </p>
    </div>
  );
}

// ── Sub-componentes ─────────────────────────────────────────────

function Card({
  title, children, tone = 'default',
}: { title: string; children: React.ReactNode; tone?: 'default' | 'red' }) {
  return (
    <div className={`bg-white rounded-2xl shadow-sm border p-5 mb-5 ${
      tone === 'red' ? 'border-red-100' : 'border-gray-100'
    }`}>
      <h2 className="text-sm font-bold text-gray-800 uppercase tracking-wider mb-4">{title}</h2>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

function ConceptRow({
  icon, name, example, desc,
}: { icon: string; name: string; example: string; desc: React.ReactNode }) {
  return (
    <div className="flex gap-3 pb-3 border-b border-gray-50 last:border-b-0 last:pb-0">
      <div className="text-2xl shrink-0">{icon}</div>
      <div className="flex-1">
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="font-bold text-gray-900">{name}</span>
          <span className="text-xs text-gray-400 italic">{example}</span>
        </div>
        <p className="text-sm text-gray-600 mt-0.5">{desc}</p>
      </div>
    </div>
  );
}

function DecisionBranch({
  letter, color, title, subtitle, warning, steps,
}: {
  letter: string;
  color: 'purple' | 'blue' | 'green';
  title: string;
  subtitle: string;
  warning: string;
  steps: React.ReactNode[];
}) {
  const colors = {
    purple: 'bg-purple-100 text-purple-700 border-purple-200',
    blue: 'bg-blue-100 text-blue-700 border-blue-200',
    green: 'bg-green-100 text-green-700 border-green-200',
  };
  return (
    <div className="border border-gray-100 rounded-xl p-4 bg-gray-50/50">
      <div className="flex items-center gap-3 mb-2">
        <div className={`w-8 h-8 rounded-full flex items-center justify-center font-bold ${colors[color]}`}>
          {letter}
        </div>
        <div>
          <h3 className="font-semibold text-gray-900 text-sm">{title}</h3>
          {subtitle && <p className="text-xs text-gray-500">{subtitle}</p>}
        </div>
      </div>
      {warning && (
        <div className="bg-amber-50 border-l-4 border-amber-400 rounded p-2 mb-3">
          <p className="text-xs text-amber-800">⚠ {warning}</p>
        </div>
      )}
      <ol className="space-y-2 ml-2">
        {steps.map((step, i) => (
          <li key={i} className="flex gap-2 text-sm text-gray-700">
            <span className="text-purple-500 font-bold shrink-0">{i + 1}.</span>
            <span className="flex-1">{step}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Rule({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex gap-2 text-sm text-gray-700">
      <span className="text-red-500 shrink-0">✗</span>
      <span>{children}</span>
    </div>
  );
}

function Faq({ q, children }: { q: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-sm font-semibold text-gray-800">{q}</p>
      <p className="text-sm text-gray-600 mt-0.5">{children}</p>
    </div>
  );
}

function Button({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-block px-2 py-0.5 text-xs font-semibold bg-purple-600 text-white rounded">
      {children}
    </span>
  );
}

function Chip({
  children, color,
}: { children: React.ReactNode; color: 'green' | 'red' | 'yellow' }) {
  const colors = {
    green: 'bg-green-100 text-green-700',
    red: 'bg-red-100 text-red-700',
    yellow: 'bg-yellow-100 text-yellow-700',
  };
  return (
    <span className={`inline-block px-1.5 py-0.5 text-[10px] font-semibold rounded ${colors[color]}`}>
      {children}
    </span>
  );
}

