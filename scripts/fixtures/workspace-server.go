package main

import (
	"log"
	"net/http"
	"os"
)

func main() {
	http.HandleFunc("/", func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/baked.txt" && request.URL.Path != "/runtime/mounted.txt" {
			http.NotFound(writer, request)
			return
		}
		content, err := os.ReadFile("/app" + request.URL.Path)
		if err != nil {
			http.Error(writer, "verification file unavailable", http.StatusInternalServerError)
			return
		}
		writer.Header().Set("Content-Type", "text/plain")
		writer.Write(content)
	})
	log.Fatal(http.ListenAndServe(":8080", nil))
}
