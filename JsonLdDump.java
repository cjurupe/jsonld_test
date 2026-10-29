import java.nio.file.Files;
import java.nio.file.Paths;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import com.manta.json.JsonHelpers;
import com.manta.jsonld.JsonLDUtils;
import com.manta.jsonld.LocalBusinessJsonLD;
import com.manta.jsonld.BreadCrumbsJsonLD;
import com.manta.jsonld.CompanyPageJsonLDGenerator;

/**
 * Small CLI to dump the JSON-LD that CompanyPageJsonLDGenerator produces
 * for a given company JSON file (the ?json=1 payload from a company page).
 *
 * Run from the repo root:
 *   java -cp "target/classes:$(cat target/cp.txt)" tools/JsonLdDump.java /tmp/company.json
 */
public class JsonLdDump {

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("Usage: JsonLdDump <company.json>");
            System.exit(1);
        }

        ObjectMapper mapper = new ObjectMapper();
        JsonNode root = mapper.readTree(Files.readAllBytes(Paths.get(args[0])));

        // El payload de ?json=1 envuelve la empresa en data.company; el generador
        // espera el nodo de la empresa a secas. Si viene envuelto, lo desenvolvemos.
        JsonNode companyData = root;
        if (root.hasNonNull("data") && root.get("data").hasNonNull("company")) {
            companyData = root.get("data").get("company");
        }

        // Manual wiring (normally done by Guice via @Inject).
        JsonHelpers jsonHelpers = new JsonHelpers();
        JsonLDUtils jsonLDUtils = new JsonLDUtils(jsonHelpers);
        LocalBusinessJsonLD localBusiness = new LocalBusinessJsonLD(jsonHelpers, jsonLDUtils);
        BreadCrumbsJsonLD breadCrumbs = new BreadCrumbsJsonLD(jsonHelpers, jsonLDUtils);
        CompanyPageJsonLDGenerator generator =
                new CompanyPageJsonLDGenerator(localBusiness, breadCrumbs);

        JsonNode jsonLd = generator.generateJsonLDForCompanyViewPage(companyData);
        System.out.println(mapper.writerWithDefaultPrettyPrinter().writeValueAsString(jsonLd));
    }
}
