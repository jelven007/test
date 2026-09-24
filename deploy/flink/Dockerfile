FROM flink:1.20.1-scala_2.12-java17

ARG KAFKA_CONNECTOR_VERSION=3.3.0-1.20

USER root
RUN mkdir -p /opt/flink/plugins/s3-fs-hadoop /opt/flink/plugins/s3-fs-presto \
    && cp /opt/flink/opt/flink-s3-fs-hadoop-*.jar /opt/flink/plugins/s3-fs-hadoop/ \
    && cp /opt/flink/opt/flink-s3-fs-presto-*.jar /opt/flink/plugins/s3-fs-presto/ \
    && curl --fail --location --retry 3 \
      "https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/${KAFKA_CONNECTOR_VERSION}/flink-sql-connector-kafka-${KAFKA_CONNECTOR_VERSION}.jar" \
      --output /opt/flink/lib/flink-sql-connector-kafka.jar

COPY deploy/flink/sql /opt/banxia/sql

USER flink
