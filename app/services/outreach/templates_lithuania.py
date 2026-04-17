from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutreachTemplate:
    template_id: str
    subject: str
    body: str


lithuania_first_touch_templates: list[OutreachTemplate] = [
    OutreachTemplate(
        template_id="lt_first_1",
        subject="Bendradarbiavimo pasiūlymas {company_name}",
        body=(
            "Sveiki,\n\n"
            "Mano vardas Jevgeni Reinas, atstovauju įmonei „Lertisento“.\n\n"
            "Pastebėjome Jūsų veiklą {industry} srityje ir norėtume pasiūlyti bendradarbiavimą darbuotojų nuomos srityje.\n\n"
            "Turime patirties tiekiant darbuotojus gamybos įmonėms: pagalbiniams darbams, teritorijos priežiūrai, "
            "atliekų tvarkymui, produkcijos paruošimui bei pagalbai operatoriams. Taip pat galime pasiūlyti darbuotojus, "
            "turinčius patirties dirbant su elektriniais vežimėliais ir krautuvais.\n\n"
            "Galime operatyviai pasiūlyti atsakingus, fiziškai pasirengusius ir motyvuotus darbuotojus, "
            "pasiruošusius dirbti pamaininiu grafiku.\n\n"
            "Šioje srityje dirbame daugiau nei 10 metų ir aktyviai bendradarbiaujame su įmonėmis Lietuvoje. "
            "Užtikriname visą procesą: nuo darbuotojų atrankos iki jų koordinavimo darbo metu.\n\n"
            "Būtų malonu aptarti bendradarbiavimo galimybes ir pasiūlyti sprendimus pagal Jūsų poreikius.\n\n"
            "Daugiau apie mūsų įmonę:\n"
            "https://www.lertisento.fi/LERTISENTO%20UAB.pdf\n\n"
            "Pagarbiai,\n\n"
            "Jevgeni Reinas\n"
            "CEO\n"
            "Lertisento UAB\n"
            "Tel.: +372 5567 1118\n"
            "El. paštas: info@lertisento.fi\n"
            "Interneto svetainė: www.lertisento.fi/lt\n"
            "LinkedIn: https://www.linkedin.com/company/lertisento-o-/"
        ),
    ),
    OutreachTemplate(
        template_id="lt_first_2",
        subject="Darbo jėgos nuomos pasiūlymas {company_name}",
        body=(
            "Sveiki,\n\n"
            "Mano vardas Jevgeni Reinas, atstovauju įmonei „Lertisento“.\n\n"
            "Jau daugiau nei 10 metų dirbame personalo teikimo srityje ir aktyviai bendradarbiaujame su įmonėmis Lietuvoje.\n\n"
            "Norėčiau pasiūlyti Jums bendradarbiavimą darbo jėgos nuomos (outsourcingo) srityje. "
            "Teikiame plataus profilio personalą gamybos, statybos, logistikos ir pramonės įmonėms.\n\n"
            "Dėl sukauptos patirties ir plačios kandidatų bazės galime operatyviai užtikrinti tiek mažesnius, "
            "tiek didesnius personalo poreikius, įskaitant komandų stiprinimą {industry} kryptyje.\n\n"
            "Prisiimame visą procesą: nuo darbuotojų atrankos ir įdarbinimo iki administravimo, "
            "užtikrindami stabilų ir patikimą personalo darbą.\n\n"
            "Daugiau informacijos apie mūsų įmonę:\n"
            "https://www.lertisento.fi/LERTISENTO%20UAB.pdf\n\n"
            "Būčiau dėkingas už galimybę aptarti galimą bendradarbiavimą ir Jūsų esamus poreikius.\n\n"
            "Pagarbiai,\n\n"
            "Jevgeni Reinas\n"
            "CEO\n"
            "Lertisento UAB\n"
            "Tel.: +372 5567 1118\n"
            "El. paštas: info@lertisento.fi\n"
            "Interneto svetainė: www.lertisento.fi/lt\n"
            "LinkedIn: https://www.linkedin.com/company/lertisento-o-/"
        ),
    ),
    OutreachTemplate(
        template_id="lt_first_3",
        subject="Trumpai dėl personalo sprendimų {company_name}",
        body=(
            "Sveiki,\n\n"
            "Kreipiuosi dėl galimo bendradarbiavimo su {company_name}. "
            "Dirbame su gamybos įmonėmis Lietuvoje, kai reikia greitai ir patikimai sustiprinti komandą.\n\n"
            "Mūsų komanda užtikrina atranką, įdarbinimą ir kasdienį koordinavimą, "
            "kad personalo klausimai netrukdytų gamybos tempui.\n\n"
            "Jei tema aktuali, galime trumpai susiskambinti ir aptarti Jūsų poreikius.\n\n"
            "Pagarbiai,\n"
            "Jevgeni Reinas"
        ),
    ),
]


lithuania_followup_templates: list[OutreachTemplate] = [
    OutreachTemplate(
        template_id="lt_followup_1",
        subject="Trumpas priminimas dėl bendradarbiavimo",
        body=(
            "Sveiki,\n\n"
            "Trumpai primenu dėl mano ankstesnio laiško apie galimą bendradarbiavimą personalo nuomos srityje.\n\n"
            "Jei šiuo metu tema ne prioritetas, suprasiu. Jei aktualu, mielai suderinsiu trumpą pokalbį Jums patogiu laiku."
        ),
    ),
    OutreachTemplate(
        template_id="lt_followup_2",
        subject="Paskutinis trumpas follow-up",
        body=(
            "Sveiki,\n\n"
            "Tai paskutinis trumpas follow-up iš mano pusės.\n\n"
            "Jei tema neaktuali, parašykite ir daugiau netrukdysiu. "
            "Jei aktualu, pasiūlykite laiką trumpam pokalbiui ir paruošiu konkretų pasiūlymą pagal Jūsų poreikį."
        ),
    ),
]
