# Run TPC-DS performance benchmark for Spark on Cloudtik cluster

## 1. Create a new Cloudtik cluster
To run TPC-DS bencbmark on Cloudtik cluster, some benchmark tools must be installed in advance.
We provide an installation script to simplify the installation of some dependencies. You only need to add the following configuration in the configuration file.
```buildoutcfg

bootstrap_commands:
    - wget -P ~/ https://raw.githubusercontent.com/oap-project/cloudtik/main/tools/spark/benchmark/scripts/bootstrap-benchmark.sh &&
        bash ~/bootstrap-benchmark.sh  --tpcds
```

## 2. Generate data
We provide two ways to generate data for TPC-DS.
### 2.1 Use spark-shell to generate data
You need to update the datagen scala script **[tpcds_datagen.scala](./scripts/tpcds_datagen.scala)** to the cluster.
Please update the following configurations according to your request:
```
val scale = "1"                   // data scale 1GB
val format = "parquet"            // support parquer or orc
val partitionTables = true        // create partitioned table
val storage = "s3a"                // support hdfs or s3
var bucket_name = "$YOUR_BUCKET_NAME"   // when storage is "s3", this value will be use.
val useDoubleForDecimal = false   // use double format instead of decimal format
```
You can use the command `cloudtik rsync` to upload the scala script.
```buildoutcfg
cloudtik rsync-up $YOUR-CONFIGURATION-YAML  $SOMEWHERE/on/local/tpcds_datagen.scala ~/benchmark/
```

Then attach to the head node and execute the following command:
```buildoutcfg
cloudtik attach $YOUR-CONFIGURATION-YAML
spark-shell -i /home/cloudtik/benchmark/tpcds_datagen.scala --jars /home/cloudtik/runtime/benchmark-tools/spark-sql-perf/target/scala-2.12/spark-sql-perf_2.12-0.5.1-SNAPSHOT.jar
```
### 2.2 Use notebook to generate data
We also provide the way that use jupyer notebook to generate data. Please upload **[tpcda_datagen-ipynb](./notebooks/tpcds_datagen.ipynb)** to the jupyter site and ***run all cells***.
Don't forget to update the following configurations according to your request:
```
val scale = "1"                   // data scale 1GB
val format = "parquet"            // support parquer or orc
val partitionTables = true        // create partitioned table
val storage = "s3a"                // support hdfs or s3
var bucket_name = "$YOUR_BUCKET_NAME"   // when storage is "s3", this value will be use.
val useDoubleForDecimal = false   // use double format instead of decimal format
```



## 3. Run TPC-DS power test

There are a notebook, or a scala script for users to easily run TPC-DS power test with Cloudtik cluster.
You need to update the following configurations according to your request on **[tpcds_power_test.ipynb](./notebooks/tpcds_power_test.ipynb)** and **[tpcds_power_test.scala](./scripts/tpcds_power_test.scala)**:
```
val scaleFactor = "1"             // data scale 1GB
val iterations = 1                // how many times to run the whole set of queries.
val format = "parquet"            // support parquer or orc
val storage = "s3a"                // support hdfs or s3
var bucket_name = "$YOUR_BUCKET_NAME"   // when storage is "s3", this value will be use.
val partitionTables = true        // create partition tables
val query_filter = Seq()          // Seq() == all queries
//val query_filter = Seq("q1-v2.4", "q2-v2.4") // run subset of queries
val randomizeQueries = false      // run queries in a random order. Recommended for parallel runs.
```
The script and notebook can run in the same way as in the previous step.

