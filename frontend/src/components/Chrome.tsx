import { Phone, Siren } from "lucide-react";
import type { ReactNode } from "react";
import type { ClinicInfo } from "../lib/types";
import { LogoOrb } from "./LogoOrb";

export function Header({ clinic, right }: { clinic: ClinicInfo | null; right?: ReactNode }) {
  return (
    <header className="topbar">
      <div className="shell topbar__inner">
        <a className="brand" href="#/">
          <LogoOrb size={42} />
          <span>
            <span className="brand__name">{clinic?.name ?? "Health AI"}</span>
            <br />
            <span className="brand__tag">Private GP · Physiotherapy · Health checks</span>
          </span>
        </a>
        <div className="topbar__meta">
          {right ?? (
            <>
              {clinic && (
                <span className={`pill ${clinic.open_now ? "pill--open" : "pill--closed"}`}>
                  <span className="dot" />
                  {clinic.open_now ? "Open now" : "Closed now"}
                </span>
              )}
              {clinic && (
                <a className="pill pill--phone" href={`tel:${clinic.phone.replace(/\s/g, "")}`}>
                  <Phone size={14} />
                  <span>{clinic.phone}</span>
                </a>
              )}
            </>
          )}
        </div>
      </div>
    </header>
  );
}

export function Footer() {
  return (
    <footer className="shell footer">
      <div className="emergency" role="note">
        <Siren size={26} />
        <p>
          <strong>In an emergency, call 999</strong> or go to your nearest A&amp;E. For urgent medical help that isn't an
          emergency, call <strong>NHS 111</strong> or visit 111.nhs.uk. If you're struggling to cope, Samaritans are
          available on <strong>116 123</strong>, free, day or night.
        </p>
      </div>
      <div className="footer__fine">
        <span>
          Our AI receptionist can book and manage appointments and answer questions about the clinic. It can't give medical
          advice.
        </span>
        <a href="#/staff">Staff</a>
      </div>
    </footer>
  );
}
