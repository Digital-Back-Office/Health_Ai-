import { ConversationProvider } from "@elevenlabs/react";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { AssistantCard } from "./components/AssistantCard";
import { Footer, Header } from "./components/Chrome";
import { ClinicCard } from "./components/ClinicCard";
import { StaffDashboard } from "./components/StaffDashboard";
import { VisitPanel } from "./components/VisitPanel";
import { api } from "./lib/api";
import { longDate, timeLabel } from "./lib/format";
import type { ClinicInfo, Slot } from "./lib/types";
import { useHumanCall } from "./lib/useHumanCall";
import { useReceptionist } from "./lib/useReceptionist";

// Dev-only UI preview; tree-shaken out of production builds.
const Preview = import.meta.env.DEV ? lazy(() => import("./components/Preview")) : null;

function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

function useClinic(): ClinicInfo | null {
  const [clinic, setClinic] = useState<ClinicInfo | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = (attempt: number) =>
      api
        .clinic()
        .then((data) => !cancelled && setClinic(data))
        .catch(() => {
          if (!cancelled && attempt < 5) window.setTimeout(() => load(attempt + 1), 1500 * (attempt + 1));
        });
    load(0);
    return () => {
      cancelled = true;
    };
  }, []);
  return clinic;
}

function PatientApp({ clinic }: { clinic: ClinicInfo | null }) {
  const human = useHumanCall();
  const r = useReceptionist({
    // Sophie started the SOS call, or is putting the patient through to reception.
    onHumanRequest: (request) => {
      if (request.kind === "sos") void human.joinSos(request.session, request.incident);
      else void human.callReception(request.session);
    },
  });

  // The SOS button: hand any conversation over straight away (no waiting for Sophie).
  const startSos = () => {
    const inConversation = r.phase === "preparing" || r.phase === "connecting" || r.phase === "live";
    void human.startSos(inConversation ? r.handOver() : Promise.resolve(null));
  };

  const pickSlot = useCallback(
    (slot: Slot) => {
      r.sendText(`I'd like ${longDate(slot.start_time)} at ${timeLabel(slot.start_time)} with ${slot.clinician_name}, please.`);
    },
    [r],
  );

  return (
    <>
      <Header clinic={clinic} />
      <main className="shell page">
        <AssistantCard r={r} clinic={clinic} human={human} onSos={startSos} />
        <div className="stack">
          <VisitPanel visit={r.visit} live={r.phase === "live"} clinic={clinic} onPickSlot={pickSlot} />
          <ClinicCard clinic={clinic} />
        </div>
      </main>
      <Footer />
    </>
  );
}

export default function App() {
  const route = useHashRoute();
  const clinic = useClinic();

  useEffect(() => {
    if (clinic) document.title = route.startsWith("#/staff") ? `Staff · ${clinic.name}` : `${clinic.name} · AI Receptionist`;
  }, [clinic, route]);

  if (route.startsWith("#/staff")) return <StaffDashboard clinic={clinic} />;
  if (Preview && route.startsWith("#/preview")) {
    return (
      <Suspense fallback={null}>
        <Preview clinic={clinic} />
      </Suspense>
    );
  }
  return (
    <ConversationProvider>
      <PatientApp clinic={clinic} />
    </ConversationProvider>
  );
}
