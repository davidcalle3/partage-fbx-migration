"""Read-only reconciliation of completed Freebox migration jobs with Partage and S3.

Run after the transfer queue finishes, before retiring the Freebox. This
verifies catalog identity and byte length, not a cryptographic source hash.
"""
import csv
import datetime
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine import ROOT, Partage, State, db
from freebox import decode_path


def check(job, partage):
    if job["state"] != "done":
        return "incomplet"
    try:
        with db(partage.path) as con:
            item=con.execute("""SELECT id,stored_name,folder_id,name,size,storage_backend
                FROM files WHERE id=?""",(job["id"],)).fetchone()
        if item is None:
            return "absent_du_catalogue"
        if (item["stored_name"] != job["stored_name"]
                or item["folder_id"] != job["folder_id"]
                or item["name"] != job["name"]
                or int(item["size"]) != int(job["size"])
                or item["storage_backend"] != "s3"):
            return "catalogue_incoherent"
        if partage.head_size(job) != int(job["size"]):
            return "taille_s3_incorrecte"
        return "valide"
    except Exception:
        return "verification_s3_indisponible"


def safe(value):
    """Keep formula-like filenames inert in CSV spreadsheet viewers."""
    string=str(value if value is not None else "")
    return "'" + string if string.lstrip().startswith(("=","+","-","@")) else string


def run():
    partage=Partage()
    state=State()
    entries=state.report()
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    target=ROOT / ("audit-migration-" + stamp + ".csv")
    ROOT.mkdir(parents=True,exist_ok=True)
    counts={}
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses=pool.map(lambda job:check(job,partage),entries)
        descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,"w",encoding="utf-8",newline="") as output:
            writer=csv.writer(output)
            writer.writerow(["id","source","nom","taille_octets","etat_transfert",
                             "resultat_verification","reprises_reseau","erreur"])
            for job, verdict in zip(entries,statuses):
                counts[verdict]=counts.get(verdict,0)+1
                try: source=decode_path(job["source"])
                except ValueError:source="[invalide]"
                writer.writerow([job["id"],safe(source),safe(job["name"]),
                                 job["size"],job["state"],verdict,job["attempts"],
                                 safe(job["error"])])
    print("Vérification du catalogue et des longueurs S3 :",len(entries),"fichiers")
    for label,count in sorted(counts.items()):
        print(" ",label+":",count)
    print("Rapport privé :",target)
    print("Note : cette vérification ne compare pas les empreintes SHA-256 des originaux.")


if __name__ == "__main__":
    run()
