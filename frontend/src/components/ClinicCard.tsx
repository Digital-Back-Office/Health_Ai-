import { Clock, Mail, MapPin, Phone, Stethoscope } from "lucide-react";
import { hhmmToLabel, mapsLink } from "../lib/format";
import type { ClinicInfo } from "../lib/types";

export function ClinicCard({ clinic }: { clinic: ClinicInfo | null }) {
  if (!clinic) {
    return (
      <section className="card">
        <div className="card__body subtle">Loading clinic information…</div>
      </section>
    );
  }
  const todayIndex = clinic.today.weekday;
  return (
    <>
      <section className="card" aria-labelledby="hours-title">
        <div className="card__header">
          <h2 id="hours-title" className="card__title">
            <Clock size={18} /> Opening hours
          </h2>
          <span className={`pill ${clinic.open_now ? "pill--open" : "pill--closed"}`}>
            <span className="dot" /> {clinic.open_now ? "Open now" : "Closed now"}
          </span>
        </div>
        <div className="card__body" style={{ display: "grid", gap: 14 }}>
          <table className="hours">
            <tbody>
              {clinic.opening_hours.map((day) => (
                <tr key={day.weekday} className={day.weekday === todayIndex ? "is-today" : undefined}>
                  <td>{day.day}</td>
                  <td>{day.open ? `${hhmmToLabel(day.open)} – ${hhmmToLabel(day.close)}` : "Closed"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="info-line">
            <MapPin size={16} />
            <span>
              {clinic.address}
              <br />
              <a href={mapsLink(clinic.address)} target="_blank" rel="noreferrer">
                Get directions
              </a>
            </span>
          </div>
          <div className="info-line">
            <Phone size={16} />
            <a href={`tel:${clinic.phone.replace(/\s/g, "")}`}>{clinic.phone}</a>
          </div>
          <div className="info-line">
            <Mail size={16} />
            <a href={`mailto:${clinic.email}`}>{clinic.email}</a>
          </div>
        </div>
      </section>

      <section className="card" aria-labelledby="services-title">
        <div className="card__header">
          <h2 id="services-title" className="card__title">
            <Stethoscope size={18} /> Services & prices
          </h2>
        </div>
        <div className="card__body">
          <ul className="services">
            {clinic.services.map((service) => (
              <li key={service.code} title={service.description}>
                <span>
                  {service.name}
                  <br />
                  <span className="subtle">{service.duration_minutes} minutes</span>
                </span>
                <span className="services__price">{service.price}</span>
              </li>
            ))}
          </ul>
          <p className="subtle" style={{ marginTop: 10 }}>
            Tests, vaccines and medication are charged separately. Most major UK insurers accepted.
          </p>
        </div>
      </section>
    </>
  );
}
