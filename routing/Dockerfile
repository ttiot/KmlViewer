# Moteur de routage GraphHopper (ADR-0007, TR7) : jar exécutable de la release GitHub, vérifié par SHA-256.
# L'image communautaire israelhikingmap/graphhopper:11.0 embarque un jar « 11.0-SNAPSHOT » (sha256 différent de
# la release) ; Maven Central ne publie que le jar léger, sans dépendances. D'où cette construction.
# Prérequis de construction : Docker Engine >= 24 (BuildKit >= 0.12 pour ADD --checksum).
# Correctifs de sécurité de l'image de base : épinglée par digest, elle ne bouge que si on change le digest
# (docker buildx imagetools inspect eclipse-temurin:21-jre-noble) : à refaire à chaque mise à jour du moteur
# et au moins chaque trimestre.
# Mise à jour : changer GH_VERSION et GH_SHA256 (somme calculée sur le jar téléchargé, à recontrôler à chaque version).
FROM eclipse-temurin:21-jre-noble@sha256:54999a956a8bf8be9af0052790a6a832bf91a62fad3199fd194c3cc28057fc62

ARG GH_VERSION=11.0
ARG GH_SHA256=b59c024afe172ec6ec85b6327006c3138ec58c7d0bcd26253d0e42853f613def

ADD --checksum=sha256:${GH_SHA256} \
    https://github.com/graphhopper/graphhopper/releases/download/${GH_VERSION}/graphhopper-web-${GH_VERSION}.jar \
    /opt/graphhopper/graphhopper-web.jar

# UID fixe (10002, l'appli est en 10001) ; /data est le volume routing-data
RUN groupadd -r -g 10002 gh && useradd -r -u 10002 -g gh gh \
    && mkdir -p /data && chown gh:gh /data \
    && chmod 0644 /opt/graphhopper/graphhopper-web.jar

COPY config.yml /opt/graphhopper/config.yml
COPY models /opt/graphhopper/models
COPY --chmod=0755 run.sh /opt/graphhopper/run.sh

USER 10002:10002
WORKDIR /opt/graphhopper
ENTRYPOINT ["/opt/graphhopper/run.sh"]
CMD ["server"]
